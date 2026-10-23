"""
Phase 1 data model for the codebase-context graph.

The graph links four kinds of knowledge — Incidents (what broke), CodeChanges
(what caused/fixed them), Decisions (the rationale), and RootCauses (why) — so a
migration can retrieve a module's connected subgraph and preserve intent.

Design notes (see CLAUDE.md):
  * Every externally-sourced row carries a natural unique key so re-running a
    backfill or replaying a webhook UPDATES rather than duplicates (idempotency).
  * Inferred edges and root causes carry provenance: who/what drew the link, a
    confidence score, an inferred -> confirmed/rejected status, and the evidence
    used. This lets the UI say "guessed at 0.6 — confirm or reject" and lets a
    migration later filter to only trusted edges.
  * `JSONField` maps to JSONB on PostgreSQL (the intended production DB) and to
    TEXT-backed JSON on SQLite, so the models run on either without change.
"""

from django.conf import settings
from django.db import models


class Setting(models.Model):
    """
    A persisted app setting (name -> value). Used to store the Anthropic API key
    once so it's reused across sessions instead of being re-supplied each time.

    Secrets live here in plaintext — fine for a local single-tenant prototype, but
    before any real deployment move to a secrets manager / encrypted field. An env
    var always takes precedence over a stored value (see services.app_settings).
    """

    name = models.CharField(max_length=100, unique=True)
    value = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls, name, default=""):
        row = cls.objects.filter(name=name).first()
        return row.value if row else default

    @classmethod
    def put(cls, name, value):
        obj, _ = cls.objects.update_or_create(name=name, defaults={"value": value})
        return obj

    def __str__(self) -> str:
        return self.name

from .managers import CausedByEdgeManager, ImplementsEdgeManager


# ---------------------------------------------------------------------------
# Abstract bases
# ---------------------------------------------------------------------------

class TimeStampedModel(models.Model):
    """Adds created/updated bookkeeping to every row."""

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class ProvenanceSource(models.TextChoices):
    HEURISTIC = "heuristic", "Heuristic"
    LLM = "llm", "LLM"
    HUMAN = "human", "Human"


class ProvenanceStatus(models.TextChoices):
    INFERRED = "inferred", "Inferred"
    CONFIRMED = "confirmed", "Confirmed"
    REJECTED = "rejected", "Rejected"


class ProvenanceModel(TimeStampedModel):
    """
    Shared provenance carried by every inferred edge and by root causes.

    `source` says whether a heuristic, an LLM, or a human drew this; `confidence`
    is a CHECK-constrained 0..1 score; `status` moves inferred -> confirmed /
    rejected as a reviewer acts; `evidence` (JSONB) records what the inference
    was based on (blame lines, matched "fixes #123" refs, prompt + model, ...).
    """

    source = models.CharField(
        max_length=16,
        choices=ProvenanceSource.choices,
        default=ProvenanceSource.HEURISTIC,
    )
    confidence = models.FloatField(
        default=1.0,
        help_text="Model/heuristic confidence in this link, 0..1.",
    )
    status = models.CharField(
        max_length=16,
        choices=ProvenanceStatus.choices,
        default=ProvenanceStatus.INFERRED,
        db_index=True,
    )
    evidence = models.JSONField(
        default=dict,
        blank=True,
        help_text="Structured evidence the inference was based on (JSONB).",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        abstract = True
        constraints = [
            models.CheckConstraint(
                condition=models.Q(confidence__gte=0.0)
                & models.Q(confidence__lte=1.0),
                name="%(app_label)s_%(class)s_confidence_0_1",
            ),
        ]

    # -- one-click review (Phase 3 capture UX) -----------------------------

    def confirm(self, reviewer=None):
        """A human accepts this inferred link/cause."""
        self._record_review(ProvenanceStatus.CONFIRMED, reviewer)

    def reject(self, reviewer=None):
        """A human rejects this inferred link/cause."""
        self._record_review(ProvenanceStatus.REJECTED, reviewer)

    def reset_review(self):
        """Return to inferred, clearing the reviewer (undo)."""
        self.status = ProvenanceStatus.INFERRED
        self.reviewed_by = None
        self.reviewed_at = None
        self.save()

    def _record_review(self, status, reviewer):
        from django.utils import timezone

        self.status = status
        # reviewer may be an AnonymousUser (unauthenticated prototype call).
        self.reviewed_by = reviewer if getattr(reviewer, "is_authenticated", False) else None
        self.reviewed_at = timezone.now()
        self.save()


# ---------------------------------------------------------------------------
# Root entity
# ---------------------------------------------------------------------------

class Provider(models.TextChoices):
    GITHUB = "github", "GitHub"
    GITLAB = "gitlab", "GitLab"
    PAGERDUTY = "pagerduty", "PagerDuty"
    INCIDENT_IO = "incident_io", "incident.io"
    JIRA = "jira", "Jira"
    MANUAL = "manual", "Manual"


class Repository(TimeStampedModel):
    """A codebase. Every other entity hangs off exactly one Repository."""

    provider = models.CharField(
        max_length=16,
        choices=Provider.choices,
        default=Provider.GITHUB,
    )
    # For GitHub this is "owner/name"; kept generic for other providers.
    full_name = models.CharField(max_length=255)
    url = models.URLField(unique=True)
    default_branch = models.CharField(max_length=255, default="main")
    # Persistent local clone that makes `git blame` (and Phase 2) possible.
    clone_path = models.CharField(max_length=1024, blank=True, default="")

    class Meta:
        verbose_name_plural = "repositories"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "full_name"],
                name="uniq_repo_provider_fullname",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.full_name} ({self.provider})"


class SessionStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    RUNNING = "running", "Running"
    SUCCEEDED = "succeeded", "Succeeded"
    FAILED = "failed", "Failed"


class Session(TimeStampedModel):
    """
    One connect-a-repo run, tracked so the frontend can show progress and resume.

    Ingestion runs in the background; this row is the progress/status the UI polls.
    Re-running is safe (idempotent), so a FAILED session can be retried and picks
    up where it left off (already-ingested commits are skipped). The GitHub token
    is deliberately NOT stored — a retry needing one re-supplies it.
    """

    repository = models.ForeignKey(
        Repository, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sessions",
    )
    url = models.CharField(max_length=1024)
    full_name = models.CharField(max_length=255, blank=True, default="")
    ingest_issues = models.BooleanField(default=True)
    limit = models.IntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=16, choices=SessionStatus.choices,
        default=SessionStatus.PENDING, db_index=True,
    )
    phase = models.CharField(max_length=64, blank=True, default="")
    commits_total = models.IntegerField(default=0)
    commits_done = models.IntegerField(default=0)
    incidents_done = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return f"Session {self.pk} {self.url} [{self.status}]"


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

class DecisionStatus(models.TextChoices):
    PROPOSED = "proposed", "Proposed"
    ACCEPTED = "accepted", "Accepted"
    DEPRECATED = "deprecated", "Deprecated"
    SUPERSEDED = "superseded", "Superseded"


class Decision(TimeStampedModel):
    """
    An ADR-style decision: the rationale and tradeoffs behind a change.

    `supersedes` is the self-referential edge from the data model — a newer
    decision that replaces an older one.
    """

    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="decisions"
    )
    # e.g. "ADR-7"; the human-facing key used in commit/PR refs.
    key = models.CharField(max_length=64)
    title = models.CharField(max_length=512)
    status = models.CharField(
        max_length=16,
        choices=DecisionStatus.choices,
        default=DecisionStatus.PROPOSED,
    )
    context = models.TextField(blank=True, default="")
    rationale = models.TextField(blank=True, default="")
    consequences = models.TextField(blank=True, default="")
    supersedes = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="superseded_by",
    )
    source_url = models.URLField(blank=True, default="")
    # When the decision was made (parsed from the ADR), for the story timeline.
    decided_at = models.DateField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "key"], name="uniq_decision_repo_key"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.key}: {self.title}"


class ChangeKind(models.TextChoices):
    COMMIT = "commit", "Commit"
    PULL_REQUEST = "pull_request", "Pull Request"


class CodeChange(TimeStampedModel):
    """A commit or a pull request — the thing that caused or fixed something."""

    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="changes"
    )
    kind = models.CharField(max_length=16, choices=ChangeKind.choices)
    provider = models.CharField(
        max_length=16, choices=Provider.choices, default=Provider.GITHUB
    )
    # Natural id within the repo: a commit SHA, or a PR number as text.
    identifier = models.CharField(max_length=255)
    title = models.CharField(max_length=1024, blank=True, default="")
    body = models.TextField(blank=True, default="")
    author_name = models.CharField(max_length=255, blank=True, default="")
    author_email = models.CharField(max_length=255, blank=True, default="")
    url = models.URLField(blank=True, default="")
    authored_at = models.DateTimeField(null=True, blank=True)
    merged_at = models.DateTimeField(null=True, blank=True)
    # For a pull request: the sha it merged as (from the GitHub API / clone).
    # This is how a PR-kind fix is resolved to a blame-able commit.
    merge_commit_sha = models.CharField(max_length=64, blank=True, default="")

    # Factual edge: which entities this change touched, straight from the diff.
    affects = models.ManyToManyField(
        "CodeEntity",
        through="AffectsEdge",
        related_name="changes",
    )
    # Inferred edge: which decisions this change implements.
    implements = models.ManyToManyField(
        Decision,
        through="ImplementsEdge",
        related_name="implemented_by",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "provider", "kind", "identifier"],
                name="uniq_change_repo_kind_identifier",
            ),
        ]
        indexes = [
            models.Index(fields=["repository", "kind"]),
        ]

    def __str__(self) -> str:
        short = self.identifier[:12] if self.kind == ChangeKind.COMMIT else self.identifier
        return f"{self.get_kind_display()} {short}"


class EntityKind(models.TextChoices):
    FILE = "file", "File"
    MODULE = "module", "Module"
    CLASS = "class", "Class"
    FUNCTION = "function", "Function"


class CodeEntity(TimeStampedModel):
    """A file / module / class / function — the unit a migration operates on."""

    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="entities"
    )
    kind = models.CharField(max_length=16, choices=EntityKind.choices)
    path = models.CharField(max_length=1024, help_text="Repo-relative file path.")
    # Qualified name within the file (blank for a file-level entity).
    name = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        verbose_name_plural = "code entities"
        constraints = [
            models.UniqueConstraint(
                fields=["repository", "kind", "path", "name"],
                name="uniq_entity_repo_kind_path_name",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.path}::{self.name}" if self.name else self.path


class IncidentStatus(models.TextChoices):
    OPEN = "open", "Open"
    RESOLVED = "resolved", "Resolved"


class Incident(TimeStampedModel):
    """Something that broke. Often sourced from PagerDuty / incident.io / Jira."""

    repository = models.ForeignKey(
        Repository, on_delete=models.CASCADE, related_name="incidents"
    )
    provider = models.CharField(
        max_length=16, choices=Provider.choices, default=Provider.GITHUB
    )
    # Natural id in the source system (issue number, PagerDuty incident id, ...).
    external_id = models.CharField(max_length=255)
    title = models.CharField(max_length=1024)
    description = models.TextField(blank=True, default="")
    severity = models.CharField(max_length=32, blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=IncidentStatus.choices,
        default=IncidentStatus.OPEN,
    )
    started_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    url = models.URLField(blank=True, default="")

    # Inferred edge: which changes caused this incident.
    caused_by = models.ManyToManyField(
        CodeChange,
        through="CausedByEdge",
        related_name="incidents_caused",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_id"],
                name="uniq_incident_provider_externalid",
            ),
        ]

    def __str__(self) -> str:
        return f"[{self.provider}:{self.external_id}] {self.title}"


class RootCause(ProvenanceModel):
    """
    Why an incident happened. Usually LLM-drafted from postmortem + diff, then
    human-confirmed — hence it carries the full provenance base.

    `explains` is a plain FK: an incident may have several contributing causes.
    """

    incident = models.ForeignKey(
        Incident, on_delete=models.CASCADE, related_name="root_causes"
    )
    category = models.CharField(max_length=64, blank=True, default="")
    summary = models.CharField(max_length=512)
    detail = models.TextField(blank=True, default="")

    def __str__(self) -> str:
        return f"RootCause: {self.summary}"


# ---------------------------------------------------------------------------
# Edge tables
# ---------------------------------------------------------------------------

class AffectsEdge(TimeStampedModel):
    """
    CodeChange --affects--> CodeEntity.

    Factual, straight from the diff, so NO provenance — but we keep the change
    type so a migration knows whether the entity was added / modified / deleted.
    """

    class ChangeType(models.TextChoices):
        ADDED = "added", "Added"
        MODIFIED = "modified", "Modified"
        DELETED = "deleted", "Deleted"
        RENAMED = "renamed", "Renamed"

    code_change = models.ForeignKey(CodeChange, on_delete=models.CASCADE)
    code_entity = models.ForeignKey(CodeEntity, on_delete=models.CASCADE)
    change_type = models.CharField(
        max_length=16, choices=ChangeType.choices, default=ChangeType.MODIFIED
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["code_change", "code_entity"],
                name="uniq_affects_change_entity",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code_change} affects {self.code_entity}"


class ImplementsEdge(ProvenanceModel):
    """CodeChange --implements--> Decision. Inferred from PR/commit refs."""

    code_change = models.ForeignKey(CodeChange, on_delete=models.CASCADE)
    decision = models.ForeignKey(Decision, on_delete=models.CASCADE)

    objects = ImplementsEdgeManager()

    class Meta(ProvenanceModel.Meta):
        constraints = ProvenanceModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["code_change", "decision"],
                name="uniq_implements_change_decision",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code_change} implements {self.decision}"


class CausedByEdge(ProvenanceModel):
    """Incident --caused_by--> CodeChange. Inferred from blame + fix references."""

    incident = models.ForeignKey(Incident, on_delete=models.CASCADE)
    code_change = models.ForeignKey(CodeChange, on_delete=models.CASCADE)

    objects = CausedByEdgeManager()

    class Meta(ProvenanceModel.Meta):
        constraints = ProvenanceModel.Meta.constraints + [
            models.UniqueConstraint(
                fields=["incident", "code_change"],
                name="uniq_causedby_incident_change",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.incident} caused_by {self.code_change}"

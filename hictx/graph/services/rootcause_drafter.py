"""
The Phase 2 LLM root-cause drafter.

For each incident we assemble context — the incident itself plus the changes
already linked as its cause (with their diffs from the clone) — ask the model for
candidate root causes, and store each as a `RootCause` with source=llm,
status=inferred, ready for one-click human confirm/reject.

Idempotency: an incident is drafted once. A re-run skips incidents that already
have LLM drafts, so we never spend tokens twice or pile up duplicates. `force`
re-drafts, but only ever clears the incident's *inferred* LLM drafts first — a
human's confirmed/rejected verdict is never destroyed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.conf import settings

from ..models import (
    ChangeKind,
    Incident,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
    RootCause,
)
from .app_settings import active_task_runner
from .git_gateway import GitError, GitGateway
from .llm import (
    ChangeContext,
    DraftContext,
    LLMClient,
    RunnerLLMClient,
)


@dataclass
class DrafterResult:
    incidents_processed: int = 0
    incidents_drafted: int = 0
    root_causes_created: int = 0
    skipped: list[str] = field(default_factory=list)


class RootCauseDrafter:
    def __init__(
        self,
        repository: Repository,
        llm_client: LLMClient | None = None,
        gateway: GitGateway | None = None,
    ):
        self.repository = repository
        # Any configured provider (Anthropic/OpenAI/Gemini) via the runner factory.
        self.llm_client = llm_client or RunnerLLMClient(active_task_runner())
        # Optional: only used to enrich the prompt with diffs when a clone exists.
        if gateway is not None:
            self.gateway = gateway
        elif repository.clone_path:
            self.gateway = GitGateway(repository.clone_path)
        else:
            self.gateway = None

    def run(self, force: bool = False) -> DrafterResult:
        result = DrafterResult()
        for incident in self.repository.incidents.all():
            result.incidents_processed += 1
            self.draft_for(incident, force, result)
        return result

    def draft_for(
        self, incident: Incident, force: bool, result: DrafterResult
    ) -> None:
        existing_llm = incident.root_causes.filter(source=ProvenanceSource.LLM)
        if existing_llm.exists():
            if not force:
                result.skipped.append(
                    f"incident {incident.external_id}: already has LLM drafts "
                    "(use --force to re-draft)"
                )
                return
            # Re-draft: clear only untouched (inferred) drafts; keep human verdicts.
            existing_llm.filter(status=ProvenanceStatus.INFERRED).delete()

        context = self._build_context(incident)
        drafts = self.llm_client.draft_root_causes(context)
        if not drafts:
            result.skipped.append(
                f"incident {incident.external_id}: model returned no drafts"
            )
            return

        for draft in drafts:
            RootCause.objects.create(
                incident=incident,
                summary=draft.summary,
                category=draft.category,
                detail=draft.detail,
                source=ProvenanceSource.LLM,
                status=ProvenanceStatus.INFERRED,
                confidence=draft.confidence,
                evidence={
                    "model": getattr(self.llm_client, "model", "stub"),
                    "inputs": self._evidence_inputs(context),
                },
            )
            result.root_causes_created += 1
        result.incidents_drafted += 1

    # -- context assembly --------------------------------------------------

    def _build_context(self, incident: Incident) -> DraftContext:
        changes = []
        edges = incident.causedbyedge_set.select_related("code_change")
        for edge in edges:
            change = edge.code_change
            changes.append(
                ChangeContext(
                    identifier=change.identifier,
                    title=change.title,
                    body=change.body,
                    diff=self._diff_for(change),
                )
            )
        return DraftContext(
            incident_title=incident.title,
            incident_description=incident.description,
            severity=incident.severity,
            changes=changes,
        )

    def _diff_for(self, change) -> str:
        if self.gateway is None or change.kind != ChangeKind.COMMIT:
            return ""
        try:
            diff = self.gateway.commit_diff(change.identifier)
        except GitError:
            return ""
        cap = settings.GRAPH_LLM_MAX_DIFF_CHARS
        return diff[:cap]

    @staticmethod
    def _evidence_inputs(context: DraftContext) -> list[str]:
        inputs = ["incident"]
        for c in context.changes:
            inputs.append(f"change:{c.identifier[:12]}")
            if c.diff:
                inputs.append(f"diff:{c.identifier[:12]}")
        return inputs

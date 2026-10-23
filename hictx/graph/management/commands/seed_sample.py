"""
Seed a small sample graph and walk it end to end.

Demonstrates the Phase 1 data model: an Incident linked (with provenance) to a
CodeChange that implements a Decision and affects a CodeEntity, plus an
LLM-drafted RootCause. Idempotent - every row is fetched by its natural key, so
re-running updates rather than duplicates.

    uv run python manage.py seed_sample
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from hictx.graph.models import (
    AffectsEdge,
    CausedByEdge,
    ChangeKind,
    CodeChange,
    CodeEntity,
    Decision,
    DecisionStatus,
    EntityKind,
    ImplementsEdge,
    Incident,
    IncidentStatus,
    Provider,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
    RootCause,
)


class Command(BaseCommand):
    help = "Seed a sample context graph and print a full traversal."

    def handle(self, *args, **options):
        now = timezone.now()

        repo, _ = Repository.objects.get_or_create(
            provider=Provider.GITHUB,
            full_name="acme/payments",
            defaults={
                "url": "https://github.com/acme/payments",
                "default_branch": "main",
            },
        )

        decision, _ = Decision.objects.get_or_create(
            repository=repo,
            key="ADR-7",
            defaults={
                "title": "Retry failed charge webhooks with exponential backoff",
                "status": DecisionStatus.ACCEPTED,
                "context": "Stripe occasionally 5xxes on webhook delivery.",
                "rationale": "Silent drops caused missed charges; retries recover.",
                "consequences": "Adds a retry queue; must be idempotent.",
            },
        )

        entity, _ = CodeEntity.objects.get_or_create(
            repository=repo,
            kind=EntityKind.FUNCTION,
            path="payments/webhooks.py",
            name="handle_charge_succeeded",
        )

        change, _ = CodeChange.objects.get_or_create(
            repository=repo,
            provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST,
            identifier="482",
            defaults={
                "title": "Add backoff retry to charge webhook (implements ADR-7)",
                "body": "Fixes #91. Retries on 5xx per ADR-7.",
                "author_name": "Dana Lee",
                "author_email": "dana@acme.example",
                "url": "https://github.com/acme/payments/pull/482",
                "authored_at": now,
                "merged_at": now,
            },
        )

        # Factual edge: change touched this entity (from the diff).
        AffectsEdge.objects.get_or_create(
            code_change=change,
            code_entity=entity,
            defaults={"change_type": AffectsEdge.ChangeType.MODIFIED},
        )

        # Inferred edge: change implements decision (matched "ADR-7" in the body).
        ImplementsEdge.objects.get_or_create(
            code_change=change,
            decision=decision,
            defaults={
                "source": ProvenanceSource.HEURISTIC,
                "confidence": 0.9,
                "status": ProvenanceStatus.INFERRED,
                "evidence": {"matched_ref": "ADR-7", "field": "pr_body"},
            },
        )

        incident, _ = Incident.objects.get_or_create(
            provider=Provider.GITHUB,
            external_id="91",
            defaults={
                "repository": repo,
                "title": "Duplicate charges after webhook retries",
                "description": "Retry logic replayed non-idempotent charges.",
                "severity": "SEV2",
                "status": IncidentStatus.RESOLVED,
                "started_at": now,
                "resolved_at": now,
                "url": "https://github.com/acme/payments/issues/91",
            },
        )

        # Inferred edge: incident caused by change (blame + "Fixes #91").
        CausedByEdge.objects.get_or_create(
            incident=incident,
            code_change=change,
            defaults={
                "source": ProvenanceSource.HEURISTIC,
                "confidence": 0.6,
                "status": ProvenanceStatus.INFERRED,
                "evidence": {"fix_reference": "Fixes #91", "blame_overlap": True},
            },
        )

        # Root cause: LLM-drafted, awaiting human confirmation.
        RootCause.objects.get_or_create(
            incident=incident,
            summary="Charge handler was not idempotent under retry",
            defaults={
                "category": "idempotency",
                "detail": "Retries re-ran the charge because no dedup key existed.",
                "source": ProvenanceSource.LLM,
                "confidence": 0.7,
                "status": ProvenanceStatus.INFERRED,
                "evidence": {"model": "claude", "inputs": ["postmortem", "diff#482"]},
            },
        )

        self._print_traversal(incident)

    def _print_traversal(self, incident):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING("\nIncident -> Change -> Decision -> RootCause\n"))
        w(f"Incident [{incident.provider}:{incident.external_id}] {incident.title}")

        for rc in incident.root_causes.all():
            w(f"  -> root cause ({rc.source} {rc.confidence:.2f} {rc.status}): {rc.summary}")

        for edge in CausedByEdge.objects.filter(incident=incident).select_related("code_change"):
            change = edge.code_change
            w(
                f"  -> caused_by ({edge.source} {edge.confidence:.2f} {edge.status}): "
                f"{change.get_kind_display()} #{change.identifier} - {change.title}"
            )
            for ie in ImplementsEdge.objects.filter(code_change=change).select_related("decision"):
                d = ie.decision
                w(
                    f"      -> implements ({ie.source} {ie.confidence:.2f} {ie.status}): "
                    f"{d.key} {d.title}"
                )
            for ae in AffectsEdge.objects.filter(code_change=change).select_related("code_entity"):
                w(f"      -> affects ({ae.change_type}): {ae.code_entity}")

        w(self.style.SUCCESS("\nSample graph seeded and traversed OK.\n"))

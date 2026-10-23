"""
Custom managers holding the write-side logic for inferred edges.

The linker re-runs often, so upserts are idempotent on the edge's natural key.
The crucial rule: once a human has confirmed or rejected an edge, a re-run must
NOT overwrite that verdict — we only refresh edges still in `inferred` status.
"""

from __future__ import annotations

from django.db import models


class InferredEdgeManager(models.Manager):
    """Shared upsert for the two provenance-bearing edge tables."""

    # Subclasses set the two FK field names forming the natural key.
    natural_key_fields: tuple[str, str] = ()

    def upsert_inferred(self, *, confidence, evidence, source=None, **key):
        """
        Create or refresh an inferred edge identified by its natural key.

        Returns (edge, created). A human-reviewed edge (status != 'inferred') is
        returned untouched so re-running the linker never undoes review.
        """
        from .models import ProvenanceSource, ProvenanceStatus

        if source is None:
            source = ProvenanceSource.HEURISTIC

        edge = self.filter(**key).first()
        if edge is None:
            edge = self.create(
                source=source,
                confidence=confidence,
                status=ProvenanceStatus.INFERRED,
                evidence=evidence,
                **key,
            )
            return edge, True

        if edge.status != ProvenanceStatus.INFERRED:
            return edge, False  # confirmed/rejected: leave the human verdict be

        edge.source = source
        edge.confidence = confidence
        edge.evidence = evidence
        edge.save(update_fields=["source", "confidence", "evidence", "updated_at"])
        return edge, False


class CausedByEdgeManager(InferredEdgeManager):
    natural_key_fields = ("incident", "code_change")


class ImplementsEdgeManager(InferredEdgeManager):
    natural_key_fields = ("code_change", "decision")

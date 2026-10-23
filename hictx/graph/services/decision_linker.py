"""
The Phase 2 change-to-decision linker.

Far cheaper than the incident linker: a change that references a decision key in
its title/body (e.g. "implements ADR-7") almost certainly implements it. We parse
those refs, match them to `Decision` rows by key, and upsert inferred
`CodeChange --implements--> Decision` edges.

No git needed — this runs purely off already-ingested change text.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    CodeChange,
    Decision,
    ImplementsEdge,
    ProvenanceSource,
    Repository,
)
from . import references


@dataclass
class DecisionLinkerResult:
    changes_processed: int = 0
    edges_created: int = 0
    edges_refreshed: int = 0
    skipped: list[str] = field(default_factory=list)


class DecisionLinker:
    def __init__(self, repository: Repository):
        self.repository = repository
        # Decision keys are case-insensitive on match; index once.
        self._decisions = {
            d.key.upper(): d for d in repository.decisions.all()
        }

    def run(self) -> DecisionLinkerResult:
        result = DecisionLinkerResult()
        for change in self.repository.changes.all():
            result.changes_processed += 1
            self._link_change(change, result)
        return result

    def _link_change(self, change: CodeChange, result: DecisionLinkerResult) -> None:
        text = f"{change.title}\n{change.body}"
        keys = references.find_adr_references(text)
        if not keys:
            return
        # "implements ADR-7" is stronger signal than a bare "ADR-7" mention.
        implements_verb = "implement" in text.lower()
        for key in keys:
            decision = self._decisions.get(key.upper())
            if decision is None:
                result.skipped.append(
                    f"change {change.identifier[:12]}: references {key} but no "
                    "such decision in repo"
                )
                continue
            self._upsert(change, decision, key, implements_verb, result)

    def _upsert(self, change, decision, key, implements_verb, result) -> None:
        confidence = 0.85 if implements_verb else 0.65
        evidence = {
            "strategy": "ref-match",
            "matched_key": key,
            "field": "title+body",
            "implements_verb": implements_verb,
        }
        _, created = ImplementsEdge.objects.upsert_inferred(
            code_change=change,
            decision=decision,
            source=ProvenanceSource.HEURISTIC,
            confidence=confidence,
            evidence=evidence,
        )
        if created:
            result.edges_created += 1
        else:
            result.edges_refreshed += 1

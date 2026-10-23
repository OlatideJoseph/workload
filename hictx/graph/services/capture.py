"""
Phase 3 capture: the lightweight "why?" prompt at PR-open time.

An engineer answers one question — "why does this change exist?" — and we turn
that sentence into structured, trusted context: a human-authored Decision plus a
confirmed `CodeChange --implements--> Decision` edge. Cheap capture beats perfect
capture (CLAUDE.md), so nothing here asks for an essay.

If the change already had an *inferred* implements edge (drawn by the decision
linker), the human's answer upgrades it to human-authored + confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction

from ..models import (
    ChangeKind,
    CodeChange,
    Decision,
    DecisionStatus,
    ImplementsEdge,
    ProvenanceSource,
    Repository,
)


@dataclass
class CaptureResult:
    change: CodeChange
    decision: Decision
    edge: ImplementsEdge
    created_decision: bool


@transaction.atomic
def capture_pr_rationale(
    *,
    repository: Repository,
    change_identifier: str,
    why: str,
    change_kind: str = ChangeKind.PULL_REQUEST,
    decision_key: str = "",
    decision_title: str = "",
    reviewer=None,
) -> CaptureResult:
    change, _ = CodeChange.objects.get_or_create(
        repository=repository,
        kind=change_kind,
        identifier=change_identifier,
        defaults={"provider": repository.provider},
    )

    key = decision_key or f"WHY-{change_kind}-{change_identifier}"
    title = decision_title or _summarize(why) or key
    decision, created_decision = Decision.objects.update_or_create(
        repository=repository,
        key=key,
        defaults={
            "title": title,
            "status": DecisionStatus.ACCEPTED,
            "rationale": why,
        },
    )

    edge, _ = ImplementsEdge.objects.get_or_create(
        code_change=change, decision=decision
    )
    edge.source = ProvenanceSource.HUMAN
    edge.confidence = 1.0
    edge.evidence = {
        **(edge.evidence or {}),
        "captured_at_pr_open": True,
        "why": why,
    }
    # confirm() persists source/confidence/evidence too (full save) and records
    # the reviewer + timestamp.
    edge.confirm(reviewer=reviewer)

    return CaptureResult(
        change=change,
        decision=decision,
        edge=edge,
        created_decision=created_decision,
    )


def _summarize(text: str, limit: int = 120) -> str:
    text = (text or "").strip().splitlines()[0] if text else ""
    return text[:limit]

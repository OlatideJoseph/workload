"""
LLM-assisted link drafting: propose change -> decision ("implements") edges that
the explicit-reference heuristic misses.

The heuristic only fires on literal refs ("implements ADR-19"); on real repos
almost nobody writes that, so the graph stays sparse (korifi: 3 edges / 4208
commits). This drafter reads a change's message (and optionally its diff) against
the repo's decisions and proposes *candidate* links — each written as an inferred,
provenance-tagged edge that lands in the review queue for one-click confirm/reject.
A drafted link is never trusted until a human confirms it, so a wrong guess is
safe: it's low-confidence, flagged, and filtered out of migration context.

Two clients behind one seam:
  * KeywordLinkDraftClient — offline, deterministic: matches a decision's
    distinctive title words in the change. Cheap, no API, never above 0.7.
  * LLMLinkDraftClient — asks a model to reason over message + diff. Richer, costs
    tokens. Used only when explicitly selected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings

from ..models import (
    ChangeKind,
    ImplementsEdge,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
)
from .git_gateway import GitError, GitGateway
from .llm import TaskRunner, _clamp_confidence, _extract_json_array

_STOP = {
    "the", "and", "for", "with", "from", "into", "that", "this", "use", "using",
    "via", "adr", "record", "decision", "decisions", "architecture", "implement",
}


@dataclass
class DraftedLink:
    decision_key: str
    confidence: float
    reason: str


@dataclass
class DecisionSummary:
    key: str
    title: str
    rationale: str
    keywords: list[str]


@dataclass
class LinkDraftResult:
    changes_processed: int = 0
    links_drafted: int = 0
    links_refreshed: int = 0
    skipped_human: int = 0
    notes: list[str] = field(default_factory=list)


class LinkDraftClient(Protocol):
    def propose_links(
        self, change_text: str, diff: str, decisions: list[DecisionSummary]
    ) -> list[DraftedLink]:
        ...


def _keywords(text: str, limit: int = 8) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{3,}", (text or "").lower())
    out: list[str] = []
    for w in words:
        if w not in _STOP and w not in out:
            out.append(w)
    return out[:limit]


def _title_phrases(title: str) -> list[str]:
    """
    Distinctive multi-word phrases from a decision title, longest first.

    Scattered single words ("resource", "guid") match almost any change and wreck
    precision, so we require an intact ≥2-word phrase from the title (e.g. "service
    credentials"). Phrases are built from maximal runs of significant words — a
    run is split by stopwords/punctuation.
    """
    run: list[str] = []
    runs: list[list[str]] = []
    for tok in re.split(r"[^a-z0-9]+", title.lower()):
        if tok and len(tok) > 3 and tok not in _STOP:
            run.append(tok)
        else:
            if len(run) >= 2:
                runs.append(run)
            run = []
    if len(run) >= 2:
        runs.append(run)

    phrases: list[str] = []
    for r in runs:
        for size in range(len(r), 1, -1):  # longest n-grams first
            for i in range(len(r) - size + 1):
                phrases.append(" ".join(r[i:i + size]))
    # longest (most specific) first, de-duplicated
    seen, out = set(), []
    for p in sorted(phrases, key=lambda s: -s.count(" ")):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


class KeywordLinkDraftClient:
    """Propose a link when a distinctive ≥2-word phrase from a decision title
    appears intact in the change (message/diff)."""

    def __init__(self, ceiling: float = 0.72):
        self.ceiling = ceiling

    def propose_links(self, change_text, diff, decisions):
        haystack = re.sub(r"\s+", " ", f"{change_text} {diff}".lower())
        out: list[DraftedLink] = []
        for d in decisions:
            matched = next((p for p in _title_phrases(d.title) if p in haystack), None)
            if matched:
                nwords = matched.count(" ") + 1
                conf = min(self.ceiling, round(0.5 + 0.1 * (nwords - 1), 2))
                out.append(DraftedLink(d.key, conf, f'title phrase: "{matched}"'))
        return out


class LLMLinkDraftClient:
    """Ask a model which decisions a change implements (reasons over message + diff)."""

    SYSTEM = (
        "You link a code change to the architecture decisions it implements or "
        "enforces. Propose a link ONLY when the change plausibly carries out a "
        "decision; be conservative. Reply with a JSON array of "
        '{"decision_key": "ADR-N", "confidence": 0.0-1.0, "reason": "<short>"}; '
        "an empty array [] if none apply. No prose outside the JSON."
    )

    def __init__(self, runner: TaskRunner):
        self.runner = runner

    def propose_links(self, change_text, diff, decisions):
        catalog = "\n".join(
            f"- {d.key}: {d.title} — {d.rationale[:240]}" for d in decisions
        )
        user = (
            f"CHANGE:\n{change_text[:1500]}\n\n"
            f"DIFF (truncated):\n{diff[:3000]}\n\n"
            f"DECISIONS:\n{catalog}\n\n"
            "Which of these decisions does this change implement? JSON array only."
        )
        valid = {d.key for d in decisions}
        out: list[DraftedLink] = []
        for item in _extract_json_array(self.runner.run(self.SYSTEM, user)):
            key = str(item.get("decision_key", "")).upper()
            if key in valid:
                out.append(DraftedLink(key, _clamp_confidence(item.get("confidence")),
                                       str(item.get("reason", ""))[:300]))
        return out


def _summaries(repository: Repository) -> list[DecisionSummary]:
    out = []
    for d in repository.decisions.all():
        # Keywords from the TITLE only: titles name the decision's subject
        # ("service credentials projection"), whereas rationale bodies are full of
        # generic implementation words ("controller", "client") that match almost
        # any change and wreck precision. The LLM client still sees the rationale.
        out.append(DecisionSummary(
            key=d.key.upper(), title=d.title, rationale=d.rationale or "",
            keywords=_keywords(d.title),
        ))
    return out


def draft_implements_links(
    repository: Repository,
    client: LinkDraftClient | None = None,
    limit: int | None = None,
    use_diff: bool = False,
    min_confidence: float = 0.5,
) -> LinkDraftResult:
    result = LinkDraftResult()
    decisions = _summaries(repository)
    if not decisions:
        result.notes.append("no decisions to link against")
        return result
    by_key = {d.key: d for d in decisions}
    client = client or KeywordLinkDraftClient()

    gateway = None
    if use_diff and repository.clone_path:
        gateway = GitGateway(repository.clone_path)

    changes = repository.changes.all().order_by("-authored_at")
    if limit:
        changes = changes[:limit]

    # Map uppercased key -> the actual Decision row, to attach edges.
    decision_rows = {d.key.upper(): d for d in repository.decisions.all()}

    for change in changes:
        result.changes_processed += 1
        diff = ""
        if gateway and change.kind == ChangeKind.COMMIT:
            try:
                diff = gateway.commit_diff(change.identifier)
            except GitError:
                diff = ""
        text = f"{change.title}\n{change.body or ''}"
        for link in client.propose_links(text, diff, decisions):
            if link.confidence < min_confidence or link.decision_key not in by_key:
                continue
            decision = decision_rows.get(link.decision_key)
            if decision is None:
                continue
            edge = ImplementsEdge.objects.filter(
                code_change=change, decision=decision
            ).first()
            if edge and edge.status in (ProvenanceStatus.CONFIRMED,
                                        ProvenanceStatus.REJECTED):
                result.skipped_human += 1
                continue
            if edge and edge.source == ProvenanceSource.HEURISTIC:
                # An explicit "ADR-N" reference is a stronger signal than a keyword
                # guess — never downgrade it to a draft.
                continue
            evidence = {"drafted_by": type(client).__name__, "reason": link.reason}
            if edge is None:
                ImplementsEdge.objects.create(
                    code_change=change, decision=decision,
                    source=ProvenanceSource.LLM, confidence=link.confidence,
                    status=ProvenanceStatus.INFERRED, evidence=evidence,
                )
                result.links_drafted += 1
            else:
                edge.source = ProvenanceSource.LLM
                edge.confidence = link.confidence
                edge.evidence = evidence
                edge.save(update_fields=["source", "confidence", "evidence", "updated_at"])
                result.links_refreshed += 1
    return result

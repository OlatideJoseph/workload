"""
Phase 5: evaluate honestly whether captured context improves an AI task.

The Phase 0 metric (CLAUDE.md): "N edge-case fixes preserved that a context-free
port drops." We operationalise it as: for each code entity that has trusted
context, list the *must-preserve items* (each decision's rationale, each past
incident + its root causes), run the task WITH and WITHOUT the context, and count
how many items each output preserves. The headline number is the DELTA —
items the with-context run kept that the context-free run dropped.

Two judges decide "did the output preserve this item?":
  * `KeywordJudge` — deterministic keyword overlap. Offline, cheap, testable; a
    conservative proxy, not a semantic judge.
  * `LLMJudge` — asks a model per item. Closer to human judgement; costs tokens.

The judge is a seam so the metric logic is tested without a model, and the harness
is honest: a stub run yields a stub number, clearly labelled.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from django.conf import settings

from ..models import CodeEntity
from .context_test import run_context_test
from .git_gateway import GitGateway
from .llm import TaskRunner
from .retrieval import EntitySubgraph


@dataclass
class PreserveItem:
    """One piece of context intent the rewrite must keep."""

    id: str
    kind: str  # "decision" | "incident" | "root_cause"
    text: str
    keywords: list[str] = field(default_factory=list)


def must_preserve_items(subgraph: EntitySubgraph) -> list[PreserveItem]:
    items: list[PreserveItem] = []
    for h in subgraph.decisions:
        text = h.decision.rationale or h.decision.title
        items.append(
            PreserveItem(f"decision:{h.decision.key}", "decision", text,
                         _keywords(f"{h.decision.title} {h.decision.rationale}"))
        )
    for h in subgraph.incidents:
        items.append(
            PreserveItem(f"incident:{h.incident.external_id}", "incident",
                         h.incident.title, _keywords(h.incident.title))
        )
        for rc in h.root_causes:
            items.append(
                PreserveItem(f"rootcause:{rc.pk}", "root_cause", rc.summary,
                             _keywords(rc.summary))
            )
    return items


class Judge(Protocol):
    def preserved(self, item: PreserveItem, output: str) -> bool:
        ...


class KeywordJudge:
    """Preserved if enough of the item's distinctive keywords appear in the output."""

    def __init__(self, min_overlap: float = 0.5):
        self.min_overlap = min_overlap

    def preserved(self, item: PreserveItem, output: str) -> bool:
        if not item.keywords:
            return False
        low = output.lower()
        hits = sum(1 for kw in item.keywords if kw in low)
        return hits / len(item.keywords) >= self.min_overlap


class LLMJudge:
    """Ask a model whether the output preserves the item's intent (costs tokens)."""

    def __init__(self, runner: TaskRunner):
        self.runner = runner

    def preserved(self, item: PreserveItem, output: str) -> bool:
        system = (
            "You judge whether an answer preserves a specific piece of intent. "
            "Reply with exactly YES or NO."
        )
        user = (
            f"INTENT TO PRESERVE:\n{item.text}\n\n"
            f"ANSWER:\n{output}\n\n"
            "Does the ANSWER preserve or account for that intent? YES or NO."
        )
        return self.runner.run(system, user).strip().upper().startswith("YES")


@dataclass
class EntityEval:
    entity_path: str
    items_total: int
    preserved_with: int
    preserved_without: int

    @property
    def delta(self) -> int:
        return self.preserved_with - self.preserved_without


@dataclass
class EvaluationReport:
    entities: list[EntityEval] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def items_total(self) -> int:
        return sum(e.items_total for e in self.entities)

    @property
    def preserved_with(self) -> int:
        return sum(e.preserved_with for e in self.entities)

    @property
    def preserved_without(self) -> int:
        return sum(e.preserved_without for e in self.entities)

    @property
    def delta(self) -> int:
        """The headline: items context preserved that a context-free run dropped."""
        return self.preserved_with - self.preserved_without


def evaluate_entity(
    entity: CodeEntity,
    source_code: str,
    runner: TaskRunner,
    judge: Judge,
    task: str = "explain",
    include_inferred: bool = True,
    min_confidence: float = 0.5,
) -> EntityEval | None:
    result = run_context_test(
        entity, source_code, task=task, runner=runner,
        include_inferred=include_inferred, min_confidence=min_confidence,
    )
    if not result.had_context:
        return None  # nothing to measure — no trusted context for this entity
    items = must_preserve_items(result.subgraph)
    preserved_with = sum(1 for it in items if judge.preserved(it, result.with_context))
    preserved_without = sum(
        1 for it in items if judge.preserved(it, result.without_context)
    )
    target = f"{entity.path}::{entity.name}" if entity.name else entity.path
    return EntityEval(target, len(items), preserved_with, preserved_without)


def evaluate_repository(
    repository,
    runner: TaskRunner,
    judge: Judge,
    task: str = "explain",
    include_inferred: bool = True,
    min_confidence: float = 0.5,
) -> EvaluationReport:
    report = EvaluationReport()
    gateway = GitGateway(repository.clone_path) if repository.clone_path else None
    for entity in repository.entities.all():
        source = _source_for(gateway, entity)
        ev = evaluate_entity(
            entity, source, runner, judge, task=task,
            include_inferred=include_inferred, min_confidence=min_confidence,
        )
        if ev is None:
            continue
        report.entities.append(ev)
    return report


def _source_for(gateway, entity) -> str:
    if gateway is not None:
        content = gateway.file_at("HEAD", entity.path)
        if content:
            cap = settings.GRAPH_LLM_MAX_DIFF_CHARS
            return content[:cap]
    return f"# source for {entity.path} unavailable\n"


def _keywords(text: str) -> list[str]:
    """Distinctive lowercase words (len>3), de-duplicated, minus common stopwords."""
    stop = {
        "this", "that", "with", "from", "into", "when", "then", "than", "have",
        "were", "will", "code", "change", "changes", "under", "after", "before",
    }
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_]{3,}", text.lower())
    out: list[str] = []
    for w in words:
        if w not in stop and w not in out:
            out.append(w)
    return out[:8]

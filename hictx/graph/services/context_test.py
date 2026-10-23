"""
Phase 4 harness: run an AI task on a code entity WITH and WITHOUT its connected
context, so the two outputs can be compared (the comparison itself is Phase 5).

The whole premise of the product rides on this: does the captured context change
what the model produces? The harness holds the source constant and varies only
whether the CONNECTED CONTEXT block is prepended, then returns both answers plus
a record of exactly what context was available.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import CodeEntity
from .app_settings import active_task_runner
from .llm import TaskRunner
from .retrieval import EntitySubgraph, render_context, retrieve_entity_subgraph

_SYSTEM = {
    "explain": (
        "You are a senior engineer. Explain what this code does, calling out any "
        "non-obvious, load-bearing, or edge-case behavior that a rewrite must "
        "preserve. If CONNECTED CONTEXT is provided, use it."
    ),
    "migrate": (
        "You are porting this code to {target}. Produce an idiomatic {target} "
        "translation that preserves all intent and hard-won edge-case handling. If "
        "CONNECTED CONTEXT is provided, preserve the decisions and avoid "
        "reintroducing the past incidents it describes."
    ),
}


@dataclass
class ContextTestResult:
    entity: CodeEntity
    task: str
    context_text: str
    had_context: bool
    without_context: str
    with_context: str
    subgraph: EntitySubgraph


def run_context_test(
    entity: CodeEntity,
    source_code: str,
    task: str = "explain",
    target_language: str = "",
    runner: TaskRunner | None = None,
    include_inferred: bool = True,
    min_confidence: float = 0.5,
) -> ContextTestResult:
    if task not in _SYSTEM:
        raise ValueError(f"unknown task {task!r}; choose from {sorted(_SYSTEM)}")
    runner = runner or active_task_runner()

    subgraph = retrieve_entity_subgraph(
        entity, include_inferred=include_inferred, min_confidence=min_confidence
    )
    had_context = not subgraph.is_empty()
    # Only inject a context block when there IS trusted context; otherwise the
    # "with" run is identical to the "without" run (nothing to add).
    context_text = render_context(subgraph) if had_context else ""

    system = _SYSTEM[task].format(target=target_language or "the target language")
    target = f"{entity.path}::{entity.name}" if entity.name else entity.path
    base = f"SOURCE ({target}):\n```\n{source_code}\n```\n\nTASK: {task} this code."

    without = runner.run(system, base)
    with_prompt = f"{context_text}\n{base}" if had_context else base
    with_ = runner.run(system, with_prompt)

    return ContextTestResult(
        entity=entity,
        task=task,
        context_text=context_text,
        had_context=had_context,
        without_context=without,
        with_context=with_,
        subgraph=subgraph,
    )

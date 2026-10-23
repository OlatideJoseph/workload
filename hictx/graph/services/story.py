"""
LLM narrative on top of the repo timeline — the repo's story in prose.

Given the chronological notable events (decisions, incidents, the changes that
carried them out), a model writes a short, grounded narrative a new engineer can
read to understand how and why the codebase evolved. Every claim is anchored to
an event we pass in; the model is told not to invent.

It needs an Anthropic key (set once in Settings). The result is cached per repo
in a Setting so it isn't regenerated on every visit — regenerate explicitly after
ingesting more history.
"""

from __future__ import annotations

from .llm import TaskRunner
from .timeline import file_timeline, repository_timeline

SYSTEM = (
    "You are writing the story of a software repository for an engineer who is new "
    "to it. You are given a chronological timeline of its NOTABLE events only — "
    "architecture decisions (ADRs), incidents, and the code changes that carried "
    "them out. Write a concise, engaging narrative in Markdown: 3–5 short "
    "paragraphs that explain how the codebase evolved and WHY, calling out the "
    "pivotal decisions and any incidents and what they changed. Ground every claim "
    "in the events provided — reference ADR keys, dates, and themes — and never "
    "invent events or facts that aren't in the timeline. No headings; just prose."
)

SYSTEM_FILE = (
    "You are explaining the story of ONE file to an engineer about to modify it. "
    "You are given a chronological timeline of the changes that touched this file, "
    "the architecture decisions those changes implement, and any incidents they "
    "caused. Write a concise narrative in Markdown: 2–4 short paragraphs on why "
    "this file looks the way it does — the decisions and hard-won fixes baked into "
    "it that a rewrite must preserve. Ground every claim in the events; reference "
    "ADR keys, dates and incidents; never invent. No headings; just prose."
)


def build_prompt(repo_full_name: str, timeline: dict) -> str:
    lines = []
    for e in timeline["events"]:
        date = (e["date"] or "undated")[:10]
        detail = (e["detail"] or "").replace("\n", " ").strip()[:160]
        line = f"{date} | {e['kind'].upper()} | {e['title']}"
        if detail:
            line += f" — {detail}"
        lines.append(line)
    span = timeline["span"]
    return (
        f"REPOSITORY: {repo_full_name}\n"
        f"SPAN: {span['from']} to {span['to']}  ({timeline['count']} notable events)\n\n"
        f"TIMELINE (chronological):\n" + "\n".join(lines) + "\n\nWrite the story."
    )


def _generate(subject: str, timeline: dict, runner: TaskRunner, system: str, empty: str) -> str:
    if not timeline["events"]:
        return empty
    return runner.run(system, build_prompt(subject, timeline)).strip()


def generate_narrative(repository, runner: TaskRunner, max_events: int = 60) -> str:
    return _generate(
        repository.full_name, repository_timeline(repository, limit=max_events),
        runner, SYSTEM,
        "There isn't a story to tell yet — this repository has no notable "
        "connected events. Ingest ADRs/incidents and draft links first.",
    )


def generate_file_narrative(entity, runner: TaskRunner, max_events: int = 60) -> str:
    label = f"{entity.path}::{entity.name}" if entity.name else entity.path
    return _generate(
        label, file_timeline(entity, limit=max_events), runner, SYSTEM_FILE,
        "No captured context for this file yet — none of the changes that touched "
        "it are linked to a decision or incident.",
    )

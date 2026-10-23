"""
Ingest Architecture Decision Records (ADRs) from a repository's clone.

ADRs are the `Decision` half of the graph, and without them the
`CodeChange --implements--> Decision` edge never forms automatically. Most repos
that keep ADRs follow the Nygard / adr-tools convention: numbered markdown files
(`0007-use-retry-backoff.md`) under a `docs/adr/` (or `.../decisions/`) directory,
each with `## Status`, `## Context`, `## Decision`, `## Consequences` sections.

Everything is read from the clone (no API), and ingestion is idempotent on the
decision key ("ADR-7"), so re-running after a `git fetch` updates rather than dupes.
The key format matches what the DecisionLinker looks for in commit/PR text, so
ingesting ADRs is what lets "implements ADR-7" references link up.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..models import Decision, DecisionStatus, Repository
from .git_gateway import GitGateway

# A path is ADR-like if it's markdown AND either lives under an adr/decisions
# directory, or its filename is a numbered ADR file.
_ADR_DIR_RE = re.compile(r"(^|/)(adr|adrs|decisions?)(/|$)", re.IGNORECASE)
_ADR_FILE_RE = re.compile(r"^(?:adr[-_]?)?(\d{1,4})[-_ ].+\.md$", re.IGNORECASE)
_NUM_PREFIX_RE = re.compile(r"^(?:adr[-_]?)?(\d{1,4})[.\-_ :]+", re.IGNORECASE)

_STATUS_MAP = {
    "proposed": DecisionStatus.PROPOSED,
    "accepted": DecisionStatus.ACCEPTED,
    "deprecated": DecisionStatus.DEPRECATED,
    "superseded": DecisionStatus.SUPERSEDED,
    "rejected": DecisionStatus.PROPOSED,  # no rejected state; keep as proposed
}

_SUPERSEDES_RE = re.compile(r"supersed(?:e|es|ed)\b[^\n]*?\bADR[-\s]?(\d+)", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _parse_date(text: str) -> str | None:
    """An ISO date for the ADR: prefer a line mentioning 'date', else the first."""
    for line in text.splitlines():
        if "date" in line.lower():
            m = _ISO_DATE_RE.search(line)
            if m:
                return m.group(1)
    m = _ISO_DATE_RE.search(text)
    return m.group(1) if m else None


@dataclass
class ADRResult:
    decisions_created: int = 0
    decisions_updated: int = 0
    files_scanned: int = 0
    supersedes_linked: int = 0
    notes: list[str] = field(default_factory=list)


def is_adr_path(path: str) -> bool:
    if not path.lower().endswith(".md"):
        return False
    name = os.path.basename(path)
    if name.lower() in {"readme.md", "index.md", "template.md"}:
        return False
    return bool(_ADR_DIR_RE.search(path)) or bool(_ADR_FILE_RE.match(name))


def _section(text: str, *headings: str) -> str:
    """Body of the first matching `## <heading>` section, trimmed."""
    for heading in headings:
        m = re.search(
            rf"^#{{1,6}}\s*{re.escape(heading)}\s*$(.*?)(?=^#{{1,6}}\s|\Z)",
            text, re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if m:
            return m.group(1).strip()
    return ""


def parse_adr(path: str, text: str) -> dict | None:
    """Pure: parse one ADR markdown file into Decision field values (or None)."""
    name = os.path.basename(path)
    file_match = _ADR_FILE_RE.match(name)

    # Title: first markdown H1/H2, with any leading number/ADR prefix stripped.
    title = ""
    heading = re.search(r"^#{1,3}\s+(.+?)\s*$", text, re.MULTILINE)
    if heading:
        title = _NUM_PREFIX_RE.sub("", heading.group(1).strip()).strip()
    if not title:
        title = _NUM_PREFIX_RE.sub("", os.path.splitext(name)[0]).replace("-", " ").strip()

    # Key: the ADR number, from filename first, else the title's heading number.
    number = None
    if file_match:
        number = int(file_match.group(1))
    else:
        hn = re.match(r"\s*(?:adr[-\s]?)?(\d{1,4})\b", (heading.group(1) if heading else ""),
                      re.IGNORECASE)
        if hn:
            number = int(hn.group(1))
    if number is None:
        return None  # can't form a stable ADR-N key; skip
    key = f"ADR-{number}"

    status_line = _section(text, "Status").splitlines()
    status = DecisionStatus.PROPOSED
    if status_line:
        first = status_line[0].strip().lower()
        for word, mapped in _STATUS_MAP.items():
            if word in first:
                status = mapped
                break

    supersedes = [f"ADR-{n}" for n in _SUPERSEDES_RE.findall(text)]

    return {
        "key": key,
        "title": title[:512] or key,
        "status": status,
        "context": _section(text, "Context")[:8000],
        "rationale": _section(text, "Decision", "Rationale")[:8000],
        "consequences": _section(text, "Consequences")[:8000],
        "decided_at": _parse_date(text),
        "supersedes_keys": supersedes,
    }


def ingest_adrs(repository: Repository, gateway: GitGateway | None = None) -> ADRResult:
    if not repository.clone_path:
        return ADRResult(notes=["no local clone for this repository"])
    gateway = gateway or GitGateway(repository.clone_path)

    result = ADRResult()
    supersedes_by_key: dict[str, list[str]] = {}

    for path in gateway.list_files("HEAD"):
        if not is_adr_path(path):
            continue
        result.files_scanned += 1
        parsed = parse_adr(path, gateway.file_at("HEAD", path))
        if not parsed:
            continue
        supersedes_by_key[parsed["key"]] = parsed.pop("supersedes_keys")
        _, created = Decision.objects.update_or_create(
            repository=repository, key=parsed["key"],
            defaults={k: v for k, v in parsed.items() if k != "key"},
        )
        result.decisions_created += int(created)
        result.decisions_updated += int(not created)

    # Second pass: wire supersedes now that all decisions exist.
    for key, targets in supersedes_by_key.items():
        if not targets:
            continue
        decision = Decision.objects.filter(repository=repository, key=key).first()
        target = Decision.objects.filter(repository=repository, key=targets[0]).first()
        if decision and target and decision.supersedes_id != target.pk:
            decision.supersedes = target
            decision.save(update_fields=["supersedes", "updated_at"])
            result.supersedes_linked += 1

    return result

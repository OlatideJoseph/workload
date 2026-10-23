"""
Pure text-parsing helpers for the Phase 2 linkers.

No Django or git imports here on purpose: these are the cheapest, most heavily
unit-tested functions in the pipeline, and keeping them pure makes the
heuristics easy to reason about and to tune.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# GitHub's issue/PR closing keywords. Matching one of these before an issue
# reference is what tells us a change is a *fix* for that incident.
_FIX_KEYWORDS = r"clos(?:e|es|ed)|fix(?:|es|ed)|resolv(?:e|es|ed)"

# "fixes #91", "Fixed #91", "resolves GH-91" (case-insensitive, flexible space).
_FIX_REFERENCE_RE = re.compile(
    rf"\b(?:{_FIX_KEYWORDS})\b\s*:?\s+(?:#|gh-)(\d+)",
    re.IGNORECASE,
)

# A bare "#91" issue mention, without a closing keyword (weaker signal).
_BARE_ISSUE_RE = re.compile(r"(?<![\w/])#(\d+)\b")

# The decision key used in ImplementsEdge inference. Matches "ADR-7", "ADR 7",
# "adr7", "ADR-004", "ADR: 4", "ADR #4", and the common filename-reference form
# "ADR `0019-service-credentials.md`" (any run of separator punctuation between
# the ADR keyword and the number). The separator class excludes letters/digits,
# so prose like "write an ADR" or "ADRs" never yields a false number.
_ADR_REFERENCE_RE = re.compile(r"\bADR[-\s:#.'\x60]*(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class IssueReference:
    """An issue number mentioned in some text, and whether it closes the issue."""

    number: str
    is_fix: bool


def find_fix_references(text: str) -> list[str]:
    """Issue numbers this text explicitly *fixes*/*closes* (e.g. 'fixes #91')."""
    if not text:
        return []
    return _dedupe(_FIX_REFERENCE_RE.findall(text))


def find_issue_references(text: str) -> list[IssueReference]:
    """
    Every issue reference in the text, tagged with whether it's a fix.

    A fix reference ("fixes #91") is strong signal; a bare mention ("see #91")
    is weak. Callers weight confidence accordingly.
    """
    if not text:
        return []
    fixes = set(find_fix_references(text))
    numbers = _dedupe(_BARE_ISSUE_RE.findall(text)) + [n for n in fixes]
    seen: set[str] = set()
    out: list[IssueReference] = []
    for number in numbers:
        if number in seen:
            continue
        seen.add(number)
        out.append(IssueReference(number=number, is_fix=number in fixes))
    return out


def find_adr_references(text: str) -> list[str]:
    """
    Decision keys referenced in the text, normalised to 'ADR-<n>' with leading
    zeros stripped — so a commit's "ADR-004" matches an ingested "ADR-4" (repos
    commonly pad the reference but the key is the bare number).
    """
    if not text:
        return []
    return _dedupe(f"ADR-{int(n)}" for n in _ADR_REFERENCE_RE.findall(text))


def _dedupe(items) -> list[str]:
    """Order-preserving de-duplication."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out

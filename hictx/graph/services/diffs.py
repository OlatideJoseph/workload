"""
Minimal unified-diff parser for SZZ-style blame targeting.

Given the diff of a *fixing* change, we want the lines in the PARENT revision
that the fix deleted or modified — those are the lines to blame to find the
commit that introduced the bug. Pure additions have no pre-image line, so they
contribute nothing (correct: brand-new code can't have a prior cause).

Only the standard `git` unified-diff shape is handled (`diff --git`, `--- a/…`,
`+++ b/…`, `@@ -a,b +c,d @@`), which is all `git show` / `git diff` emit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_OLD_PATH_RE = re.compile(r"^--- (?:a/)?(.*)$")


@dataclass
class FileChange:
    """Pre-image line numbers a fix touched in one file of the parent revision."""

    path: str
    # 1-based line numbers in the PARENT that were deleted/modified by the fix.
    preimage_lines: list[int] = field(default_factory=list)


def parse_preimage_lines(diff_text: str) -> list[FileChange]:
    """
    Parse a unified diff into per-file lists of touched parent line numbers.

    Files that are purely added (`--- /dev/null`) are skipped, as are hunks with
    no deletions.
    """
    files: list[FileChange] = []
    current: FileChange | None = None
    old_ln = 0
    in_hunk = False

    for line in diff_text.splitlines():
        old_path = _OLD_PATH_RE.match(line) if line.startswith("--- ") else None
        if old_path:
            raw = old_path.group(1).strip()
            in_hunk = False
            if raw in ("/dev/null", ""):
                current = None  # newly added file: no pre-image to blame
            else:
                current = FileChange(path=raw)
                files.append(current)
            continue

        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m and current is not None:
                old_ln = int(m.group(1))
                in_hunk = True
            else:
                in_hunk = False
            continue

        if not in_hunk or current is None:
            continue

        if line.startswith("-") and not line.startswith("---"):
            current.preimage_lines.append(old_ln)  # deleted/modified line
            old_ln += 1
        elif line.startswith("+"):
            pass  # addition: consumes no parent line
        else:
            old_ln += 1  # context line

    return [f for f in files if f.preimage_lines]

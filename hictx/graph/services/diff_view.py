"""
Resolve a CodeChange to its unified diff, from the repository's local clone.

A commit's diff is `git show <sha>`. A pull request has no diff of its own in the
clone unless we recorded the sha it merged as (`merge_commit_sha`) — so PRs are
resolvable only when that's known. Everything is read from the persistent clone,
so this never touches the GitHub API.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings

from ..models import ChangeKind
from .git_gateway import GitError, GitGateway


@dataclass
class ChangeDiff:
    change_id: int
    identifier: str
    kind: str
    sha: str
    available: bool
    diff: str
    truncated: bool
    reason: str = ""


def change_diff(change, max_chars: int | None = None) -> ChangeDiff:
    max_chars = max_chars or settings.GRAPH_LLM_MAX_DIFF_CHARS
    repo = change.repository
    base = ChangeDiff(
        change_id=change.pk, identifier=change.identifier, kind=change.kind,
        sha="", available=False, diff="", truncated=False,
    )

    if not repo.clone_path:
        base.reason = "no local clone for this repository"
        return base

    # Which commit carries the diff?
    if change.kind == ChangeKind.COMMIT:
        sha = change.identifier
    elif change.merge_commit_sha:
        sha = change.merge_commit_sha
    else:
        base.reason = "pull request has no recorded merge commit to diff"
        return base

    try:
        raw = GitGateway(repo.clone_path).commit_diff(sha)
    except GitError as exc:
        base.sha = sha
        base.reason = f"could not read diff: {exc}"
        return base

    truncated = len(raw) > max_chars
    base.sha = sha
    base.available = True
    base.diff = raw[:max_chars]
    base.truncated = truncated
    return base

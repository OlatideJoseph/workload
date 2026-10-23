"""
GitHub pull-request adapter.

The clone gives us commits, but for squash-merge repos the "why" — the closing
references ("fixes #123") and the rationale — lives in the PULL REQUEST, not the
commit. This adapter fetches PRs via the API and stores each as a CodeChange
(kind=pull_request) carrying:

  * body   — where `fixes #N` / ADR references live, so the linkers can fire.
  * merge_commit_sha — the commit the PR merged as, which is how the incident
    linker blames it and how the diff viewer resolves a PR's diff.

Needs a token for any real volume (PRs are API-only). Idempotent on the PR number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from django.utils.dateparse import parse_datetime

from ..models import ChangeKind, CodeChange, Provider, Repository
from .github_issues import GitHubIssuesClient


@dataclass
class PRResult:
    prs_created: int = 0
    prs_updated: int = 0
    merged_with_sha: int = 0
    notes: list[str] = field(default_factory=list)


class PRsClient(Protocol):
    def fetch_pulls(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        ...


def map_pr_to_change_fields(pr: dict) -> dict:
    """Pure: turn one GitHub PR payload into CodeChange field values."""
    return {
        "title": (pr.get("title") or "")[:1024],
        "body": pr.get("body") or "",
        "author_name": (pr.get("user") or {}).get("login", ""),
        "url": pr.get("html_url", ""),
        "authored_at": parse_datetime(pr["created_at"]) if pr.get("created_at") else None,
        "merged_at": parse_datetime(pr["merged_at"]) if pr.get("merged_at") else None,
        "merge_commit_sha": pr.get("merge_commit_sha") or "",
    }


def ingest_pull_requests(
    repository: Repository,
    token: str = "",
    state: str = "all",
    client: PRsClient | None = None,
) -> PRResult:
    owner, _, repo = repository.full_name.partition("/")
    if not repo:
        raise ValueError(f"{repository.full_name!r} is not 'owner/repo'")
    client = client or GitHubIssuesClient(token=token)

    result = PRResult()
    for pr in client.fetch_pulls(owner, repo, state=state):
        fields = map_pr_to_change_fields(pr)
        _, created = CodeChange.objects.update_or_create(
            repository=repository,
            provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST,
            identifier=str(pr.get("number")),
            defaults=fields,
        )
        if created:
            result.prs_created += 1
        else:
            result.prs_updated += 1
        if fields["merge_commit_sha"]:
            result.merged_with_sha += 1
    return result

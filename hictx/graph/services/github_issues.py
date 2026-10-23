"""
GitHub issues -> Incidents adapter.

The clone gives us commits/diffs/files for free, but incidents live in the issues
API. This adapter fetches issues and upserts them as `Incident` rows so the
incident->commit linker has something to work from.

A token is optional for public repos (raises the rate limit from 60 to 5000/hr)
and required for private ones. The HTTP call sits behind `IssuesClient` so the
upsert logic is unit-tested offline with canned issue payloads.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from django.utils.dateparse import parse_datetime

from ..models import Incident, IncidentStatus, Provider, Repository


@dataclass
class IssuesResult:
    incidents_created: int = 0
    incidents_updated: int = 0
    skipped_pull_requests: int = 0
    notes: list[str] = field(default_factory=list)


class _PaginationExhausted(Exception):
    """GitHub refuses deep page-based pagination on very large datasets (422)."""


class RateLimited(Exception):
    """GitHub returned 403/429 — rate limit hit (raise the limit with a token)."""


class IssuesClient(Protocol):
    def fetch_issues(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        ...


class GitHubIssuesClient:
    """Real client over the GitHub REST API (stdlib urllib — no new dependency)."""

    def __init__(self, token: str = "", per_page: int = 50, max_pages: int = 20):
        self.token = token
        self.per_page = per_page
        self.max_pages = max_pages

    def fetch_issues(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        return self._paginate("issues", owner, repo, state)

    def fetch_pulls(self, owner: str, repo: str, state: str = "all") -> list[dict]:
        return self._paginate("pulls", owner, repo, state)

    def _paginate(self, endpoint: str, owner: str, repo: str, state: str) -> list[dict]:
        items: list[dict] = []
        for page in range(1, self.max_pages + 1):
            url = (
                f"https://api.github.com/repos/{owner}/{repo}/{endpoint}"
                f"?state={state}&per_page={self.per_page}&page={page}"
            )
            try:
                batch = self._get(url)
            except _PaginationExhausted:
                # Huge backlog: keep the pages we did get rather than failing.
                break
            if not batch:
                break
            items.extend(batch)
            if len(batch) < self.per_page:
                break
        return items

    def fetch_issue(self, owner: str, repo: str, number: str) -> dict | None:
        """One issue by number. Returns None if it doesn't exist (404)."""
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "hictx-context-graph",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 410):  # missing or deleted/transferred — skip
                return None
            if exc.code in (403, 429):
                raise RateLimited() from exc
            raise RuntimeError(f"GitHub API {exc.code} for {url}") from exc
        except (urllib.error.URLError, http.client.HTTPException,
                json.JSONDecodeError, OSError):
            return None  # transient — skip this one

    def _get(self, url: str) -> list[dict]:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "hictx-context-graph",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")

        # GitHub's large issue payloads occasionally cut off mid-read
        # (IncompleteRead) or briefly drop the connection. Retry a few times,
        # and always surface the final failure as RuntimeError so callers'
        # (RuntimeError, ValueError) handling keeps the rest of the session alive.
        last_exc = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    return json.load(resp)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                if exc.code == 422 and b"Pagination" in body:
                    raise _PaginationExhausted() from exc
                raise RuntimeError(
                    f"GitHub API {exc.code} for {url}: {body[:200]!r}"
                ) from exc
            except (
                urllib.error.URLError,
                http.client.HTTPException,  # includes IncompleteRead
                json.JSONDecodeError,
                ConnectionError,
                TimeoutError,
                OSError,
            ) as exc:
                last_exc = exc  # transient — back off and retry
                time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"GitHub API read failed after retries: {last_exc}")


def map_issue_to_incident_fields(issue: dict) -> dict:
    """Pure: turn one GitHub issue payload into Incident field values."""
    closed = issue.get("state") == "closed"
    labels = [lbl.get("name", "") for lbl in issue.get("labels", [])]
    severity = next(
        (lbl for lbl in labels if lbl.lower() in {"sev1", "sev2", "sev3", "critical",
                                                  "major", "minor"}),
        "",
    )
    return {
        "title": issue.get("title", "")[:1024],
        "description": issue.get("body") or "",
        "status": IncidentStatus.RESOLVED if closed else IncidentStatus.OPEN,
        "severity": severity,
        "started_at": parse_datetime(issue["created_at"]) if issue.get("created_at") else None,
        "resolved_at": parse_datetime(issue["closed_at"]) if issue.get("closed_at") else None,
        "url": issue.get("html_url", ""),
    }


def ingest_issues(repository: Repository, issues: list[dict]) -> IssuesResult:
    """Upsert Incidents from raw issue payloads. Idempotent on (provider, number)."""
    result = IssuesResult()
    for issue in issues:
        if "pull_request" in issue:
            # The issues endpoint also returns PRs; those are CodeChanges, not incidents.
            result.skipped_pull_requests += 1
            continue
        number = str(issue.get("number"))
        _, created = Incident.objects.update_or_create(
            provider=Provider.GITHUB,
            external_id=number,
            defaults={"repository": repository, **map_issue_to_incident_fields(issue)},
        )
        if created:
            result.incidents_created += 1
        else:
            result.incidents_updated += 1
    return result


def seed_incidents_from_fix_refs(
    repository: Repository,
    client: "GitHubIssuesClient | None" = None,
    max_issues: int = 60,
) -> IssuesResult:
    """
    Targeted issue backfill: fetch exactly the issues the commits say they fix.

    Scans ingested commits for "fixes #N" references, then fetches those specific
    issues (a handful of calls, not thousands of pages) and ingests them as
    incidents. These are the issues most likely to actually link — so this is
    what makes a graph appear for a real repo, and it stays cheap on rate limits.
    """
    from . import references

    client = client or GitHubIssuesClient()
    owner, _, repo = repository.full_name.partition("/")
    if not repo:
        raise ValueError(f"{repository.full_name!r} is not 'owner/repo'")

    numbers: list[str] = []
    seen: set[str] = set()
    for change in repository.changes.all().iterator():
        text = f"{change.title}\n{change.body}"
        for number in references.find_fix_references(text):
            if number not in seen:
                seen.add(number)
                numbers.append(number)

    result = IssuesResult()
    for number in numbers[:max_issues]:
        try:
            issue = client.fetch_issue(owner, repo, number)
        except RateLimited:
            result.notes.append("rate limited — add a GitHub token for the rest")
            break
        if issue is None:
            continue
        if "pull_request" in issue:
            result.skipped_pull_requests += 1
            continue
        _, created = Incident.objects.update_or_create(
            provider=Provider.GITHUB,
            external_id=str(issue.get("number")),
            defaults={"repository": repository, **map_issue_to_incident_fields(issue)},
        )
        if created:
            result.incidents_created += 1
        else:
            result.incidents_updated += 1
    return result


def ingest_repository_issues(
    repository: Repository,
    token: str = "",
    state: str = "all",
    client: IssuesClient | None = None,
) -> IssuesResult:
    owner, _, repo = repository.full_name.partition("/")
    if not repo:
        raise ValueError(
            f"repository.full_name {repository.full_name!r} is not 'owner/repo'"
        )
    client = client or GitHubIssuesClient(token=token)
    issues = client.fetch_issues(owner, repo, state=state)
    return ingest_issues(repository, issues)

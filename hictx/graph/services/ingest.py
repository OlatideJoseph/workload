"""
Clone-based backfill: ingest a repository's history straight from a git clone.

The key point: a public repo's commits, diffs, and file changes are all in the
clone itself — no API token required (only issue/PR *metadata* needs the API).
So we can clone any open repo and populate CodeChange (commits), CodeEntity
(files), and the factual CodeChange--affects-->CodeEntity edges, which is enough
to run the decision linker, blame/SZZ, retrieval, and the context test on real data.

Idempotent: commits key on their sha, files on their path, so re-running after a
`git fetch` updates rather than duplicates (CLAUDE.md).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from django.conf import settings
from django.utils.dateparse import parse_datetime

from ..models import (
    AffectsEdge,
    ChangeKind,
    CodeChange,
    CodeEntity,
    EntityKind,
    Provider,
    Repository,
)
from .git_gateway import GitGateway, clone_or_fetch

_CHANGE_TYPE = {c.value: c.value for c in AffectsEdge.ChangeType}


@dataclass
class IngestResult:
    repository: Repository = None
    commits_ingested: int = 0
    entities_touched: int = 0
    affects_edges: int = 0
    notes: list[str] = field(default_factory=list)


def derive_full_name(url: str) -> str:
    """owner/repo from a GitHub-style URL; parent/name for a local path."""
    cleaned = url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    # Split on path and scp-style separators; drop the URL scheme token.
    parts = [p for p in re.split(r"[/:\\]", cleaned) if p
             and not p.lower().startswith(("http:", "https:"))]
    if not parts:
        return url
    if len(parts) == 1:
        return parts[-1]
    return f"{parts[-2]}/{parts[-1]}"


def clone_path_for(full_name: str) -> str:
    safe = full_name.replace("/", "__").replace(":", "_")
    return os.path.join(settings.GRAPH_CLONE_ROOT, safe)


def ingest_repository(
    url: str,
    full_name: str = "",
    provider: str = Provider.GITHUB,
    branch: str = "HEAD",
    limit: int | None = None,
    clone: bool = True,
    progress=None,
    on_clone_done=None,
) -> IngestResult:
    full_name = full_name or derive_full_name(url)
    dest = clone_path_for(full_name)

    if clone:
        clone_or_fetch(url, dest)

    repository, _ = Repository.objects.update_or_create(
        provider=provider,
        full_name=full_name,
        defaults={"url": url if url.startswith("http") else f"file://{url}",
                  "clone_path": dest},
    )
    if on_clone_done:
        on_clone_done(repository)  # lets the caller record the repo before backfill

    result = IngestResult(repository=repository)
    backfill_from_clone(
        repository, branch=branch, limit=limit, result=result, progress=progress
    )
    return result


def backfill_from_clone(
    repository: Repository,
    branch: str = "HEAD",
    limit: int | None = None,
    gateway: GitGateway | None = None,
    result: IngestResult | None = None,
    progress=None,
    skip_existing: bool = True,
) -> IngestResult:
    """
    Walk history and ingest commits/files/affects.

    `progress(done, total)` is called as work proceeds (for the Session UI).
    `skip_existing` makes a re-run resume: commits already ingested are skipped,
    so retrying a failed session only does the remaining work.
    """
    gateway = gateway or GitGateway(repository.clone_path)
    result = result or IngestResult(repository=repository)

    shas = gateway.iter_commit_shas(branch, limit)
    total = len(shas)
    already = set()
    if skip_existing:
        already = set(
            CodeChange.objects.filter(
                repository=repository, kind=ChangeKind.COMMIT
            ).values_list("identifier", flat=True)
        )

    for index, sha in enumerate(shas, start=1):
        if progress and (index % 20 == 0 or index == total):
            progress(index, total)
        if sha in already:
            continue
        meta = gateway.commit_meta(sha)
        CodeChange.objects.update_or_create(
            repository=repository,
            provider=repository.provider,
            kind=ChangeKind.COMMIT,
            identifier=sha,
            defaults={
                "title": meta.subject,
                "body": meta.body,
                "author_name": meta.author_name,
                "author_email": meta.author_email,
                # parse_datetime handles "Z" and offsets; fromisoformat rejects
                # "Z" on Python < 3.11.
                "authored_at": parse_datetime(meta.authored_at) if meta.authored_at else None,
            },
        )
        change = CodeChange.objects.get(
            repository=repository, kind=ChangeKind.COMMIT, identifier=sha
        )
        result.commits_ingested += 1

        for change_type, path in gateway.name_status(sha):
            entity, _ = CodeEntity.objects.get_or_create(
                repository=repository, kind=EntityKind.FILE, path=path, name=""
            )
            AffectsEdge.objects.update_or_create(
                code_change=change,
                code_entity=entity,
                defaults={"change_type": _CHANGE_TYPE.get(change_type, "modified")},
            )
            result.entities_touched += 1
            result.affects_edges += 1

    return result

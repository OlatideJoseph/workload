"""
The Phase 2 incident-to-commit linker (SZZ-lite).

For each incident we find the change that *fixed* it (via a "fixes #N" reference),
then blame the parent of that fix over the exact lines it touched to find the
commit(s) that *introduced* the bug. Each candidate becomes an inferred
`Incident --caused_by--> CodeChange` edge with a confidence proportional to how
much of the fix it accounts for, plus JSONB evidence for the confirm/reject UI.

This is the make-or-break heuristic (CLAUDE.md), so every edge is honest about
its uncertainty: a heuristic never gets confidence 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db.models import Q
from django.utils.dateparse import parse_datetime

from . import references
from ..models import (
    ChangeKind,
    CodeChange,
    Incident,
    Provider,
    ProvenanceSource,
    Repository,
)
from . import diffs
from .git_gateway import CommitMeta, GitError, GitGateway


@dataclass
class LinkerResult:
    incidents_processed: int = 0
    incidents_with_fix: int = 0
    edges_created: int = 0
    edges_refreshed: int = 0
    skipped: list[str] = field(default_factory=list)


class IncidentCommitLinker:
    def __init__(self, repository: Repository, gateway: GitGateway | None = None):
        self.repository = repository
        self.gateway = gateway or GitGateway(repository.clone_path)

    # -- public ------------------------------------------------------------

    def run(self) -> LinkerResult:
        result = LinkerResult()
        for incident in self.repository.incidents.all():
            result.incidents_processed += 1
            self.link_incident(incident, result)
        return result

    def link_incident(self, incident: Incident, result: LinkerResult) -> None:
        fixes = self._find_fixing_changes(incident)
        if fixes:
            result.incidents_with_fix += 1
        for fix in fixes:
            self._link_from_fix(incident, fix, result)

    # -- steps -------------------------------------------------------------

    def _find_fixing_changes(self, incident: Incident) -> list[CodeChange]:
        """Changes in this repo whose text fixes the incident's external id."""
        token = f"#{incident.external_id}"
        candidates = self.repository.changes.filter(
            Q(title__icontains=token) | Q(body__icontains=token)
        )
        return [
            c
            for c in candidates
            if incident.external_id in references.find_fix_references(
                f"{c.title}\n{c.body}"
            )
        ]

    def _link_from_fix(
        self, incident: Incident, fix: CodeChange, result: LinkerResult
    ) -> None:
        fix_sha = self._resolve_fix_sha(fix)
        if fix_sha is None:
            result.skipped.append(
                f"incident {incident.external_id}: fix {fix.identifier} has no "
                "resolvable commit sha"
            )
            return

        try:
            parent = self.gateway.parent(fix_sha)
        except GitError as exc:
            result.skipped.append(f"fix {fix_sha[:12]}: {exc}")
            return
        if parent is None:
            result.skipped.append(f"fix {fix_sha[:12]}: root commit, nothing before")
            return

        diff_text = self.gateway.commit_diff(fix_sha)
        file_changes = diffs.parse_preimage_lines(diff_text)

        # Tally blamed lines per introducing commit across all touched files.
        counts: dict[str, int] = {}
        files_by_sha: dict[str, set[str]] = {}
        total = 0
        for fc in file_changes:
            blame = self.gateway.blame_file(parent, fc.path)
            for ln in fc.preimage_lines:
                sha = blame.get(ln)
                if not sha or sha == fix_sha:
                    continue
                counts[sha] = counts.get(sha, 0) + 1
                files_by_sha.setdefault(sha, set()).add(fc.path)
                total += 1

        if total == 0:
            result.skipped.append(
                f"fix {fix_sha[:12]}: no blame attribution (new code only?)"
            )
            return

        for sha, count in counts.items():
            self._upsert_edge(
                incident=incident,
                fix=fix,
                fix_sha=fix_sha,
                causing_sha=sha,
                count=count,
                total=total,
                files=sorted(files_by_sha[sha]),
                result=result,
            )

    # -- helpers -----------------------------------------------------------

    def _resolve_fix_sha(self, fix: CodeChange) -> str | None:
        """
        The blame-able sha for a fixing change.

        A commit's identifier IS its sha. A pull request resolves to its recorded
        merge commit (from the GitHub API at ingest time). Either way we verify
        the sha actually exists in the local clone before trusting it.
        """
        if fix.kind == ChangeKind.COMMIT:
            rev = fix.identifier
        elif fix.kind == ChangeKind.PULL_REQUEST and fix.merge_commit_sha:
            rev = fix.merge_commit_sha
        else:
            return None  # PR without a known merge commit: can't blame it
        try:
            return self.gateway.resolve(rev)
        except GitError:
            return None

    def _upsert_edge(
        self, *, incident, fix, fix_sha, causing_sha, count, total, files, result
    ) -> None:
        causing = self._get_or_create_commit(causing_sha)
        if causing.pk == fix.pk:
            return  # a change never causes the incident it fixes

        share = count / total
        confidence = round(min(0.9, 0.4 + 0.5 * share), 2)
        evidence = {
            "strategy": "szz-blame",
            "fix_change_id": fix.pk,
            "fix_sha": fix_sha,
            "causing_sha": causing_sha,
            "blamed_lines": count,
            "total_blamed_lines": total,
            "files": files,
        }
        from ..models import CausedByEdge

        _, created = CausedByEdge.objects.upsert_inferred(
            incident=incident,
            code_change=causing,
            source=ProvenanceSource.HEURISTIC,
            confidence=confidence,
            evidence=evidence,
        )
        if created:
            result.edges_created += 1
        else:
            result.edges_refreshed += 1

    def _get_or_create_commit(self, sha: str) -> CodeChange:
        """Map a blamed sha to a CodeChange, ingesting a stub from the clone."""
        existing = self.repository.changes.filter(
            kind=ChangeKind.COMMIT, identifier=sha
        ).first()
        if existing:
            return existing
        meta = self.gateway.commit_meta(sha)
        change, _ = CodeChange.objects.get_or_create(
            repository=self.repository,
            provider=self.repository.provider,
            kind=ChangeKind.COMMIT,
            identifier=sha,
            defaults=self._meta_to_defaults(meta),
        )
        return change

    @staticmethod
    def _meta_to_defaults(meta: CommitMeta) -> dict:
        # parse_datetime handles both the "Z" suffix and numeric offsets, and
        # returns None (not a crash) on anything unparseable — unlike
        # datetime.fromisoformat, which rejects "Z" on Python < 3.11.
        authored_at = parse_datetime(meta.authored_at) if meta.authored_at else None
        return {
            "title": meta.subject,
            "body": meta.body,
            "author_name": meta.author_name,
            "author_email": meta.author_email,
            "authored_at": authored_at,
        }

"""
"Create a session for a repo": connect a repo and assemble its context graph.

A Session row tracks one run so the frontend can show progress and resume:
  clone -> backfill commits (with a live count) -> ingest issues -> done.

`run_session` executes the phases and updates the Session as it goes; it's safe
to call again on a FAILED (or interrupted) session — ingestion is idempotent and
already-ingested commits are skipped, so a retry resumes rather than restarts.
`start_session` runs it on a background thread so the request returns immediately.
"""

from __future__ import annotations

import threading

from django.db import connections

from ..models import Session, SessionStatus
from .decision_linker import DecisionLinker
from .github_issues import (
    GitHubIssuesClient,
    ingest_repository_issues,
    seed_incidents_from_fix_refs,
)
from .adr_ingest import ingest_adrs
from .github_prs import ingest_pull_requests
from .incident_linker import IncidentCommitLinker
from .ingest import ingest_repository
from .link_drafter import draft_implements_links


def start_session(session: Session, token: str = "") -> None:
    """Run the session on a background thread (fire-and-forget)."""
    thread = threading.Thread(
        target=_run_in_thread, args=(session.pk, token), daemon=True
    )
    thread.start()


def _run_in_thread(session_id: int, token: str) -> None:
    try:
        session = Session.objects.get(pk=session_id)
        run_session(session, token=token)
    finally:
        # A spawned thread gets its own DB connections; close them so they
        # don't leak for the life of the process.
        connections.close_all()


def run_session(session: Session, token: str = "") -> Session:
    """Execute (or resume) a session synchronously, updating its progress."""
    _update(session, status=SessionStatus.RUNNING, phase="cloning", error="")

    try:
        def on_clone_done(repo):
            _update(session, repository=repo, phase="backfilling commits")

        def progress(done, total):
            _update(session, commits_done=done, commits_total=total)

        result = ingest_repository(
            url=session.url,
            full_name=session.full_name,
            limit=session.limit,
            progress=progress,
            on_clone_done=on_clone_done,
        )
        repo = result.repository

        # ADRs come straight from the clone (no token). Ingesting them is what
        # lets "implements ADR-7" references in commits/PRs link to a Decision.
        _update(session, phase="reading decisions")
        try:
            ingest_adrs(repo)
        except Exception as exc:  # noqa: BLE001
            _update(session, error=f"ADRs not ingested: {exc}")

        if session.ingest_issues and "/" in repo.full_name:
            # PRs first: for squash-merge repos the "fixes #N" refs and rationale
            # live in the PR body, and each PR carries the merge sha the linker
            # blames. Ingesting them before issues lets the fix-ref seed see them.
            _update(session, phase="ingesting pull requests")
            try:
                ingest_pull_requests(repo, token=token)
            except Exception as exc:  # noqa: BLE001
                _update(session, error=f"PRs not ingested: {exc}")

            _update(session, phase="ingesting issues")
            try:
                issues = ingest_repository_issues(repo, token=token)
                incidents = issues.incidents_created + issues.incidents_updated
                # Also fetch the specific issues the commits say they fix — the
                # ones most likely to link — since the paged list can be all PRs.
                seeded = seed_incidents_from_fix_refs(
                    repo, client=GitHubIssuesClient(token=token)
                )
                incidents += seeded.incidents_created
                _update(session, incidents_done=incidents)
            except Exception as exc:  # noqa: BLE001
                # Issues are optional: keep the successful clone/backfill and
                # note the failure rather than failing the whole session.
                _update(session, error=f"issues not ingested: {exc}")

        # Run the heuristic linkers so a graph actually appears: without this,
        # connecting a repo ingests commits/files but draws no edges, and the
        # graph view (which shows only the linked core) stays empty. LLM
        # root-cause drafting is left out here — it needs a key and costs tokens.
        _update(session, phase="linking")
        try:
            IncidentCommitLinker(repo).run()
            DecisionLinker(repo).run()
        except Exception as exc:  # noqa: BLE001
            _update(session, error=f"linking incomplete: {exc}")

        # Draft the change->decision links the explicit-reference heuristic misses,
        # so the review queue has candidates to confirm. Offline keyword client
        # (message-only) — no token, no cost; the LLM client is opt-in via command.
        _update(session, phase="drafting links")
        try:
            draft_implements_links(repo)
        except Exception as exc:  # noqa: BLE001
            _update(session, error=f"link drafting incomplete: {exc}")

        _update(session, phase="done", status=SessionStatus.SUCCEEDED)
    except Exception as exc:  # noqa: BLE001 — surface any failure to the UI
        _update(session, status=SessionStatus.FAILED, error=str(exc))
    return session


def _update(session: Session, **fields) -> None:
    for key, value in fields.items():
        setattr(session, key, value)
    session.save(update_fields=[*fields.keys(), "updated_at"])

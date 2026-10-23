"""
Tests for the connect-a-repo session runner: progress, terminal states, and
idempotent resume. Runs `run_session` synchronously (no thread) against a real
local git repo so it's deterministic.
"""

import os
import shutil
import subprocess
import tempfile

from django.test import TestCase, override_settings

from hictx.graph.models import (
    ChangeKind,
    CodeChange,
    Session,
    SessionStatus,
)
from hictx.graph.services.session import run_session


def git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.co",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.co",
    }
    out = subprocess.run(["git", *args], cwd=repo, env=env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class SessionRunnerTests(TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-src-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)
        git(self.src, "init", "-q")
        for i in range(3):
            path = os.path.join(self.src, f"f{i}.py")
            with open(path, "w") as fh:
                fh.write(f"x = {i}\n")
            git(self.src, "add", "-A")
            git(self.src, "commit", "-q", "-m", f"commit {i}")

    def _session(self):
        return Session.objects.create(url=self.src, ingest_issues=False)

    def test_success_records_progress_and_terminal_state(self):
        session = self._session()
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            run_session(session)
        session.refresh_from_db()
        self.assertEqual(session.status, SessionStatus.SUCCEEDED)
        self.assertEqual(session.phase, "done")
        self.assertEqual(session.commits_total, 3)
        self.assertEqual(session.commits_done, 3)
        self.assertIsNotNone(session.repository)
        self.assertEqual(
            CodeChange.objects.filter(kind=ChangeKind.COMMIT).count(), 3
        )

    def test_failure_sets_failed_status_and_error(self):
        session = Session.objects.create(url="/no/such/repo/path", ingest_issues=False)
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            run_session(session)
        session.refresh_from_db()
        self.assertEqual(session.status, SessionStatus.FAILED)
        self.assertTrue(session.error)

    def test_retry_resumes_without_duplicating(self):
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            run_session(self._session())
            # A second session over the same repo re-runs idempotently.
            second = self._session()
            run_session(second)
        second.refresh_from_db()
        self.assertEqual(second.status, SessionStatus.SUCCEEDED)
        self.assertEqual(second.commits_total, 3)
        self.assertEqual(
            CodeChange.objects.filter(kind=ChangeKind.COMMIT).count(), 3
        )

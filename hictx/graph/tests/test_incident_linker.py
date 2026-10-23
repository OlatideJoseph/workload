"""
Integration test for the SZZ-lite incident-to-commit linker against a real,
throwaway git repository. This exercises the actual `git blame` path — the part
that decides whether the whole idea works — not a mock.
"""

import os
import shutil
import subprocess
import tempfile

from django.test import SimpleTestCase, TestCase

from hictx.graph.models import (
    CausedByEdge,
    ChangeKind,
    CodeChange,
    Incident,
    IncidentStatus,
    Provider,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services.git_gateway import CommitMeta
from hictx.graph.services.incident_linker import IncidentCommitLinker


class CommitDateParsingTests(SimpleTestCase):
    """Regression: git/GitHub timestamps may end in 'Z', which
    datetime.fromisoformat rejects on Python < 3.11."""

    def test_z_suffix_authored_at_parses(self):
        meta = CommitMeta("sha", "A", "a@e", "2026-02-23T10:40:42Z", "subj", "body")
        defaults = IncidentCommitLinker._meta_to_defaults(meta)
        self.assertIsNotNone(defaults["authored_at"])
        self.assertEqual(defaults["authored_at"].year, 2026)

    def test_offset_suffix_authored_at_parses(self):
        meta = CommitMeta("sha", "A", "a@e", "2026-02-23T10:40:42+00:00", "s", "")
        self.assertIsNotNone(
            IncidentCommitLinker._meta_to_defaults(meta)["authored_at"]
        )

BUGGY = "def divide(a, b):\n    result = a / b\n    return result\n"
FIXED = "def divide(a, b):\n    result = a / b if b else None\n    return result\n"


def git(repo, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    out = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


class IncidentCommitLinkerTests(TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hictx-git-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

        git(self.dir, "init", "-q")
        path = os.path.join(self.dir, "foo.py")

        with open(path, "w") as fh:
            fh.write(BUGGY)
        git(self.dir, "add", "foo.py")
        git(self.dir, "commit", "-q", "-m", "Add divide helper")
        self.cause_sha = git(self.dir, "rev-parse", "HEAD")

        with open(path, "w") as fh:
            fh.write(FIXED)
        git(self.dir, "add", "foo.py")
        git(self.dir, "commit", "-q", "-m", "Guard against zero divisor. Fixes #91")
        self.fix_sha = git(self.dir, "rev-parse", "HEAD")

        self.repo = Repository.objects.create(
            provider=Provider.GITHUB,
            full_name="acme/mathlib",
            url="https://github.com/acme/mathlib",
            clone_path=self.dir,
        )
        self.incident = Incident.objects.create(
            repository=self.repo,
            provider=Provider.GITHUB,
            external_id="91",
            title="ZeroDivisionError in divide()",
            status=IncidentStatus.RESOLVED,
        )
        # The already-ingested fixing commit that closes the incident.
        self.fix = CodeChange.objects.create(
            repository=self.repo,
            provider=Provider.GITHUB,
            kind=ChangeKind.COMMIT,
            identifier=self.fix_sha,
            title="Guard against zero divisor. Fixes #91",
        )

    def _run(self):
        return IncidentCommitLinker(self.repo).run()

    def test_links_incident_to_introducing_commit(self):
        result = self._run()

        self.assertEqual(result.incidents_with_fix, 1)
        self.assertEqual(result.edges_created, 1)

        edge = CausedByEdge.objects.get(incident=self.incident)
        # The causing change is the commit that introduced the buggy line...
        self.assertEqual(edge.code_change.identifier, self.cause_sha)
        # ...ingested as a stub from the clone, not the fix itself.
        self.assertNotEqual(edge.code_change_id, self.fix.pk)
        self.assertEqual(edge.status, ProvenanceStatus.INFERRED)
        self.assertEqual(edge.source, "heuristic")
        self.assertGreaterEqual(edge.confidence, 0.4)
        self.assertLessEqual(edge.confidence, 0.9)
        self.assertEqual(edge.evidence["strategy"], "szz-blame")
        self.assertEqual(edge.evidence["causing_sha"], self.cause_sha)

    def test_idempotent_rerun_refreshes_not_duplicates(self):
        self._run()
        second = self._run()
        self.assertEqual(second.edges_created, 0)
        self.assertEqual(second.edges_refreshed, 1)
        self.assertEqual(
            CausedByEdge.objects.filter(incident=self.incident).count(), 1
        )

    def test_human_verdict_is_preserved_on_rerun(self):
        self._run()
        edge = CausedByEdge.objects.get(incident=self.incident)
        edge.status = ProvenanceStatus.CONFIRMED
        edge.confidence = 0.99
        edge.save()

        self._run()

        edge.refresh_from_db()
        self.assertEqual(edge.status, ProvenanceStatus.CONFIRMED)
        self.assertEqual(edge.confidence, 0.99)  # not clobbered by the heuristic

    def test_no_fix_reference_means_no_edge(self):
        self.fix.title = "Unrelated refactor"
        self.fix.save()
        result = self._run()
        self.assertEqual(result.incidents_with_fix, 0)
        self.assertEqual(CausedByEdge.objects.count(), 0)

    def test_pull_request_fix_resolves_via_merge_commit(self):
        # A PR-kind fix carries no sha in `identifier`; it resolves through the
        # recorded merge commit instead.
        self.fix.delete()
        pr = CodeChange.objects.create(
            repository=self.repo,
            provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST,
            identifier="482",
            title="Guard against zero divisor. Fixes #91",
            merge_commit_sha=self.fix_sha,
        )
        result = self._run()

        self.assertEqual(result.edges_created, 1)
        edge = CausedByEdge.objects.get(incident=self.incident)
        self.assertEqual(edge.code_change.identifier, self.cause_sha)
        self.assertNotEqual(edge.code_change_id, pr.pk)

    def test_pull_request_without_merge_commit_is_skipped(self):
        self.fix.delete()
        CodeChange.objects.create(
            repository=self.repo,
            provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST,
            identifier="482",
            title="Guard against zero divisor. Fixes #91",
        )
        result = self._run()
        self.assertEqual(result.edges_created, 0)
        self.assertTrue(any("no resolvable commit sha" in s for s in result.skipped))

"""
Ingestion tests. The integration test clones a real local git repo and backfills
it, exercising clone_or_fetch + the history walk without needing network access
(git clones happily from a local path, the same code path as an HTTPS URL).
"""

import os
import shutil
import subprocess
import tempfile

from django.test import SimpleTestCase, TestCase, override_settings

from hictx.graph.models import (
    AffectsEdge,
    ChangeKind,
    CodeChange,
    CodeEntity,
    Repository,
)
from hictx.graph.services import ingest


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


class DeriveFullNameTests(SimpleTestCase):
    def test_https_url(self):
        self.assertEqual(
            ingest.derive_full_name("https://github.com/acme/payments.git"),
            "acme/payments",
        )

    def test_https_url_no_dotgit(self):
        self.assertEqual(
            ingest.derive_full_name("https://github.com/acme/payments"),
            "acme/payments",
        )

    def test_scp_style(self):
        self.assertEqual(
            ingest.derive_full_name("git@github.com:acme/payments.git"),
            "acme/payments",
        )


class BackfillIntegrationTests(TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-src-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)

        git(self.src, "init", "-q")
        self._commit("a.py", "print(1)\n", "Add a. Fixes #1")
        self._commit("b.py", "print(2)\n", "Add b (implements ADR-3)")
        self._commit("a.py", "print(11)\n", "Tweak a")

    def _commit(self, path, content, message):
        with open(os.path.join(self.src, path), "w") as fh:
            fh.write(content)
        git(self.src, "add", path)
        git(self.src, "commit", "-q", "-m", message)

    def _ingest(self, **kwargs):
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            return ingest.ingest_repository(url=self.src, **kwargs)

    def test_backfill_populates_commits_files_and_edges(self):
        result = self._ingest(full_name="acme/local")

        self.assertEqual(result.commits_ingested, 3)
        repo = Repository.objects.get(full_name="acme/local")
        self.assertTrue(os.path.isdir(os.path.join(repo.clone_path, ".git")))
        self.assertEqual(
            CodeChange.objects.filter(repository=repo, kind=ChangeKind.COMMIT).count(), 3
        )
        # a.py and b.py ingested as file entities.
        self.assertEqual(
            set(CodeEntity.objects.filter(repository=repo).values_list("path", flat=True)),
            {"a.py", "b.py"},
        )
        # a.py touched by two commits -> two affects edges on it.
        a = CodeEntity.objects.get(repository=repo, path="a.py")
        self.assertEqual(AffectsEdge.objects.filter(code_entity=a).count(), 2)

    def test_commit_body_and_message_captured(self):
        self._ingest(full_name="acme/local")
        fix = CodeChange.objects.get(title__startswith="Add a")
        self.assertIn("#1", fix.title)  # message available for the linkers

    def test_idempotent_reingest(self):
        self._ingest(full_name="acme/local")
        # Re-run without cloning again (already present) — no duplicates.
        self._ingest(full_name="acme/local", clone=False)
        self.assertEqual(
            CodeChange.objects.filter(kind=ChangeKind.COMMIT).count(), 3
        )
        self.assertEqual(AffectsEdge.objects.count(), 3)  # a×2 + b×1


class EmptyRepoTests(TestCase):
    """A repo with no commits (unborn HEAD) must ingest to 0, not crash."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-empty-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)
        git(self.src, "init", "-q")  # no commits

    def test_empty_repo_ingests_zero_commits(self):
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            result = ingest.ingest_repository(url=self.src, full_name="acme/empty")
        self.assertEqual(result.commits_ingested, 0)
        self.assertEqual(CodeChange.objects.count(), 0)


class StaleCloneTests(TestCase):
    """A leftover, non-empty clone dir (from a failed clone) must be repaired,
    not crash git clone with 'destination already exists and is not empty'."""

    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-src-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)
        git(self.src, "init", "-q")
        with open(os.path.join(self.src, "a.py"), "w") as fh:
            fh.write("x = 1\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "init")

    def test_stale_nonempty_dest_is_reclonged(self):
        dest = os.path.join(self.clone_root, "acme__stale")
        os.makedirs(dest)
        with open(os.path.join(dest, "leftover.txt"), "w") as fh:
            fh.write("junk from a failed clone")
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            result = ingest.ingest_repository(url=self.src, full_name="acme/stale")
        self.assertEqual(result.commits_ingested, 1)
        self.assertFalse(os.path.exists(os.path.join(dest, "leftover.txt")))

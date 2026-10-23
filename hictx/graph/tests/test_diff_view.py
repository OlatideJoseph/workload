"""Tests for resolving a CodeChange to its diff, against a real local clone."""

import os
import shutil
import subprocess
import tempfile

from django.test import TestCase, override_settings

from hictx.graph.models import ChangeKind, CodeChange, Provider, Repository
from hictx.graph.services import ingest
from hictx.graph.services.diff_view import change_diff


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


class ChangeDiffTests(TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-src-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)
        git(self.src, "init", "-q")
        with open(os.path.join(self.src, "app.py"), "w") as fh:
            fh.write("def divide(a, b):\n    return a / b\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "add divide")

        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            ingest.ingest_repository(url=self.src, full_name="acme/local")
        self.repo = Repository.objects.get(full_name="acme/local")

    def test_commit_diff_is_available(self):
        commit = CodeChange.objects.filter(kind=ChangeKind.COMMIT).first()
        result = change_diff(commit)
        self.assertTrue(result.available)
        self.assertIn("def divide", result.diff)
        self.assertEqual(result.sha, commit.identifier)

    def test_pull_request_without_merge_commit_is_unavailable(self):
        pr = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="7",
        )
        result = change_diff(pr)
        self.assertFalse(result.available)
        self.assertIn("merge commit", result.reason)

    def test_pull_request_with_merge_commit_resolves(self):
        sha = CodeChange.objects.filter(kind=ChangeKind.COMMIT).first().identifier
        pr = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="8", merge_commit_sha=sha,
        )
        result = change_diff(pr)
        self.assertTrue(result.available)
        self.assertIn("def divide", result.diff)

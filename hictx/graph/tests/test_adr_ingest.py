"""
ADR ingestion: pure parsing + a real-clone integration test that proves the whole
point — an ingested ADR plus a commit that says "implements ADR-7" produces an
`implements` edge automatically.
"""

import os
import shutil
import subprocess
import tempfile

from django.test import SimpleTestCase, TestCase, override_settings

from hictx.graph.models import (
    ChangeKind,
    Decision,
    DecisionStatus,
    ImplementsEdge,
    Repository,
)
from hictx.graph.services import adr_ingest
from hictx.graph.services.adr_ingest import is_adr_path, parse_adr
from hictx.graph.services.decision_linker import DecisionLinker
from hictx.graph.services.ingest import ingest_repository

ADR_7 = """# 7. Retry failed charge webhooks with exponential backoff

## Status
Accepted

## Context
Stripe occasionally 5xxes on webhook delivery.

## Decision
Retry on 5xx with exponential backoff and a dedup key.

## Consequences
Adds a retry queue; handlers must be idempotent.
"""

ADR_12 = """# 12. Move retries to a durable queue

## Status
Accepted

Supersedes ADR-7.
"""


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


class AdrPathTests(SimpleTestCase):
    def test_recognises_numbered_and_dir_based_adrs(self):
        self.assertTrue(is_adr_path("docs/adr/0007-retry.md"))
        self.assertTrue(is_adr_path("doc/architecture/decisions/0001-record.md"))
        self.assertTrue(is_adr_path("0042-something.md"))

    def test_rejects_non_adrs(self):
        self.assertFalse(is_adr_path("README.md"))
        self.assertFalse(is_adr_path("src/app.py"))
        self.assertFalse(is_adr_path("docs/adr/README.md"))


class ParseAdrTests(SimpleTestCase):
    def test_parses_key_title_status_sections(self):
        d = parse_adr("docs/adr/0007-retry.md", ADR_7)
        self.assertEqual(d["key"], "ADR-7")
        self.assertEqual(d["title"], "Retry failed charge webhooks with exponential backoff")
        self.assertEqual(d["status"], DecisionStatus.ACCEPTED)
        self.assertIn("5xxes", d["context"])
        self.assertIn("dedup key", d["rationale"])
        self.assertIn("idempotent", d["consequences"])
        self.assertEqual(d["supersedes_keys"], [])

    def test_parses_supersedes_reference(self):
        d = parse_adr("docs/adr/0012-queue.md", ADR_12)
        self.assertEqual(d["key"], "ADR-12")
        self.assertEqual(d["supersedes_keys"], ["ADR-7"])

    def test_unnumberable_file_is_skipped(self):
        self.assertIsNone(parse_adr("docs/adr/notes.md", "# Just notes\n"))


class AdrIntegrationTests(TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp(prefix="hictx-adr-src-")
        self.clone_root = tempfile.mkdtemp(prefix="hictx-clones-")
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.clone_root, ignore_errors=True)
        git(self.src, "init", "-q")
        os.makedirs(os.path.join(self.src, "docs", "adr"))
        self._write("docs/adr/0007-retry.md", ADR_7)
        self._write("docs/adr/0012-queue.md", ADR_12)
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "add ADRs")
        # A code change whose message references the decision.
        self._write("payments/webhooks.py", "def handle(): ...\n")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "Add backoff retry (implements ADR-7)")

    def _write(self, rel, text):
        full = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write(text)

    def test_ingest_creates_decisions_and_links_supersedes(self):
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            repo = ingest_repository(url=self.src, full_name="acme/pay").repository
            result = adr_ingest.ingest_adrs(repo)
        self.assertEqual(result.decisions_created, 2)
        self.assertEqual(result.supersedes_linked, 1)
        adr12 = Decision.objects.get(repository=repo, key="ADR-12")
        self.assertEqual(adr12.supersedes.key, "ADR-7")

    def test_adr_plus_reference_forms_implements_edge(self):
        with override_settings(GRAPH_CLONE_ROOT=self.clone_root):
            repo = ingest_repository(url=self.src, full_name="acme/pay").repository
            adr_ingest.ingest_adrs(repo)
            DecisionLinker(repo).run()
        edge = ImplementsEdge.objects.get(decision__key="ADR-7")
        self.assertEqual(edge.code_change.kind, ChangeKind.COMMIT)
        self.assertEqual(edge.status, "inferred")

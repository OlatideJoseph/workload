from datetime import date, datetime, timezone as dt_tz

from django.test import TestCase

from hictx.graph.models import (
    AffectsEdge,
    ChangeKind,
    CodeChange,
    CodeEntity,
    Decision,
    EntityKind,
    ImplementsEdge,
    Provider,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services import mcp_server as m


class McpToolTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="payments/webhooks.py",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="Add backoff retry",
            authored_at=datetime(2023, 2, 1, tzinfo=dt_tz.utc),
        )
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.entity)
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry failed charge webhooks",
            rationale="Stripe 5xxes; retry with exponential backoff and a dedup key.",
            decided_at=date(2023, 1, 5),
        )
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

    def test_list_repositories(self):
        self.assertIn("acme/payments", m.list_repositories_text())

    def test_file_context_returns_the_why(self):
        out = m.file_context_text("acme/payments", "payments/webhooks.py")
        self.assertIn("ADR-7", out)
        self.assertIn("dedup key", out)  # the rationale, the load-bearing detail

    def test_file_context_resolves_by_suffix(self):
        out = m.file_context_text("payments", "webhooks.py")  # partial repo + file
        self.assertIn("ADR-7", out)

    def test_file_context_unknown_repo(self):
        self.assertIn("No repository", m.file_context_text("nope/nope", "x.py"))

    def test_file_context_unknown_file(self):
        out = m.file_context_text("acme/payments", "does/not/exist.py")
        self.assertIn("No file matching", out)

    def test_file_context_file_without_context(self):
        CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="unrelated.py")
        out = m.file_context_text("acme/payments", "unrelated.py")
        self.assertIn("No captured context", out)

    def test_repo_story_lists_events(self):
        out = m.repo_story_text("acme/payments")
        self.assertIn("ADR-7", out)
        self.assertIn("Add backoff retry", out)

    def test_file_story(self):
        out = m.file_story_text("acme/payments", "payments/webhooks.py")
        self.assertIn("payments/webhooks.py", out)
        self.assertIn("ADR-7", out)

    def test_find_files(self):
        out = m.find_files_text("acme/payments", "webhooks")
        self.assertIn("payments/webhooks.py", out)

    def test_tools_are_registered(self):
        # The MCP surface exposes the expected tools.
        import asyncio
        names = {t.name for t in asyncio.run(m.mcp.list_tools())}
        self.assertEqual(
            names,
            {"list_repositories", "get_file_context", "get_file_story",
             "get_repo_story", "find_files"},
        )

from datetime import date, datetime, timezone as dt_tz
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from hictx.graph.models import (
    ChangeKind,
    CodeChange,
    Decision,
    ImplementsEdge,
    Provider,
    ProvenanceStatus,
    Repository,
    Setting,
)
from hictx.graph.services.story import build_prompt, generate_narrative
from hictx.graph.services.timeline import repository_timeline


class FakeRunner:
    def __init__(self, text="A grounded story about ADR-7."):
        self.text = text
        self.seen = None

    def run(self, system, user):
        self.seen = (system, user)
        return self.text


class NarrativeServiceTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            rationale="retry with backoff", decided_at=date(2023, 1, 5),
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="Add backoff retry",
            authored_at=datetime(2023, 2, 1, tzinfo=dt_tz.utc),
        )
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

    def test_prompt_contains_events(self):
        prompt = build_prompt(self.repo.full_name, repository_timeline(self.repo))
        self.assertIn("ADR-7", prompt)
        self.assertIn("2023-01-05", prompt)
        self.assertIn("DECISION", prompt)

    def test_generate_uses_runner(self):
        runner = FakeRunner()
        out = generate_narrative(self.repo, runner)
        self.assertEqual(out, "A grounded story about ADR-7.")
        self.assertIn("ADR-7", runner.seen[1])

    def test_empty_repo_returns_note_not_llm_call(self):
        empty = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/empty",
            url="https://github.com/acme/empty",
        )
        runner = FakeRunner()
        out = generate_narrative(empty, runner)
        self.assertIsNone(runner.seen)  # no LLM call
        self.assertIn("no notable", out.lower())


class StoryEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )

    def test_get_returns_null_when_never_generated(self):
        r = self.client.get(f"/api/repositories/{self.repo.pk}/story/")
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["narrative"])

    def test_post_without_key_is_rejected(self):
        import os
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("ANTHROPIC_API_KEY", None)
            r = self.client.post(f"/api/repositories/{self.repo.pk}/story/")
            self.assertEqual(r.status_code, 400)

    def test_get_returns_cached_narrative(self):
        Setting.put(f"story_narrative:{self.repo.pk}", "cached story")
        r = self.client.get(f"/api/repositories/{self.repo.pk}/story/")
        self.assertEqual(r.json()["narrative"], "cached story")
        self.assertIsNotNone(r.json()["generated_at"])

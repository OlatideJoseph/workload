from datetime import date, datetime, timezone as dt_tz

from django.test import TestCase
from rest_framework.test import APIClient

from hictx.graph.models import (
    AffectsEdge,
    CausedByEdge,
    ChangeKind,
    CodeChange,
    CodeEntity,
    Decision,
    EntityKind,
    ImplementsEdge,
    Incident,
    Provider,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services.story import generate_file_narrative
from hictx.graph.services.timeline import file_timeline


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=dt_tz.utc)


class FileStoryTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="webhooks.py",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="Add backoff retry", authored_at=dt(2023, 2, 1),
        )
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.entity)
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            rationale="retry with backoff", decided_at=date(2023, 1, 5),
        )
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB, external_id="91",
            title="Duplicate charges", started_at=dt(2023, 3, 2),
        )
        CausedByEdge.objects.create(
            incident=self.incident, code_change=self.change,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

    def test_file_timeline_pulls_in_decisions_and_incidents(self):
        t = file_timeline(self.entity)
        kinds = {e["kind"] for e in t["events"]}
        self.assertEqual(kinds, {"change", "decision", "incident"})
        self.assertEqual(t["entity"], "webhooks.py")
        change = next(e for e in t["events"] if e["kind"] == "change")
        self.assertIn("ADR-7", change["links"])
        self.assertIn("incident #91", change["links"])

    def test_unrelated_file_has_no_events(self):
        other = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="unrelated.py",
        )
        self.assertEqual(file_timeline(other)["count"], 0)

    def test_narrative_empty_file_skips_llm(self):
        other = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="unrelated.py",
        )

        class Boom:
            def run(self, *a):
                raise AssertionError("should not call the model")

        out = generate_file_narrative(other, Boom())
        self.assertIn("no captured context", out.lower())


class FileStoryEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="t", authored_at=dt(2023, 2, 1),
        )
        self.e1 = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="a.py")
        self.e2 = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="b.py")
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.e1)
        # e2 is touched too but by an unconnected change
        self.unconnected = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="b" * 40, title="x", authored_at=dt(2023, 1, 1),
        )
        AffectsEdge.objects.create(code_change=self.unconnected, code_entity=self.e2)
        decision = Decision.objects.create(
            repository=self.repo, key="ADR-1", title="d")
        ImplementsEdge.objects.create(
            code_change=self.change, decision=decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

    def test_story_files_lists_only_connected_files_ranked(self):
        rows = self.client.get(f"/api/repositories/{self.repo.pk}/story-files/").json()
        paths = [r["path"] for r in rows]
        self.assertIn("a.py", paths)       # touched by the connected change
        self.assertNotIn("b.py", paths)    # only touched by an unconnected change
        self.assertEqual(rows[0]["context_count"], 1)

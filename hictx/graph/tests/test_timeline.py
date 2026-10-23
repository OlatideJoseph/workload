from datetime import date, datetime, timezone as dt_tz

from django.test import TestCase

from hictx.graph.models import (
    CausedByEdge,
    ChangeKind,
    CodeChange,
    Decision,
    ImplementsEdge,
    Incident,
    Provider,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services.timeline import repository_timeline


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=dt_tz.utc)


class TimelineTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            rationale="retry with backoff", decided_at=date(2023, 1, 5),
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB, external_id="91",
            title="Duplicate charges", started_at=dt(2023, 3, 2), severity="SEV2",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="Add backoff retry", authored_at=dt(2023, 2, 1),
        )
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )
        CausedByEdge.objects.create(
            incident=self.incident, code_change=self.change,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

    def test_events_ordered_by_date_with_all_kinds(self):
        t = repository_timeline(self.repo)
        kinds = [e["kind"] for e in t["events"]]
        self.assertEqual(kinds, ["decision", "change", "incident"])  # Jan, Feb, Mar
        self.assertEqual(t["count"], 3)
        self.assertEqual(t["span"]["from"][:10], "2023-01-05")
        self.assertEqual(t["span"]["to"][:10], "2023-03-02")

    def test_change_lists_its_links(self):
        t = repository_timeline(self.repo)
        change = next(e for e in t["events"] if e["kind"] == "change")
        self.assertIn("ADR-7", change["links"])
        self.assertIn("incident #91", change["links"])

    def test_unlinked_change_excluded(self):
        CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="b" * 40, title="Unrelated", authored_at=dt(2023, 4, 1),
        )
        t = repository_timeline(self.repo)
        self.assertFalse(any(e["title"] == "Unrelated" for e in t["events"]))

    def test_decision_date_falls_back_to_earliest_implementing_change(self):
        self.decision.decided_at = None
        self.decision.save()
        t = repository_timeline(self.repo)
        decision = next(e for e in t["events"] if e["kind"] == "decision")
        self.assertEqual(decision["date"][:10], "2023-02-01")  # the change's date

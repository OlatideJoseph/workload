from django.test import SimpleTestCase, TestCase

from hictx.graph.models import (
    Incident,
    IncidentStatus,
    Provider,
    Repository,
)
from hictx.graph.services import github_issues
from hictx.graph.services.github_issues import (
    ingest_issues,
    ingest_repository_issues,
    map_issue_to_incident_fields,
)


def issue(number, state="open", title="t", body="b", labels=None, pr=False, closed_at=None):
    d = {
        "number": number, "state": state, "title": title, "body": body,
        "labels": [{"name": n} for n in (labels or [])],
        "created_at": "2026-01-01T00:00:00Z",
        "closed_at": closed_at,
        "html_url": f"https://github.com/acme/payments/issues/{number}",
    }
    if pr:
        d["pull_request"] = {"url": "..."}
    return d


class MapIssueTests(SimpleTestCase):
    def test_open_issue(self):
        f = map_issue_to_incident_fields(issue(5))
        self.assertEqual(f["status"], IncidentStatus.OPEN)
        self.assertIsNone(f["resolved_at"])

    def test_closed_issue_with_severity_label(self):
        f = map_issue_to_incident_fields(
            issue(5, state="closed", labels=["bug", "SEV2"],
                  closed_at="2026-02-02T00:00:00Z")
        )
        self.assertEqual(f["status"], IncidentStatus.RESOLVED)
        self.assertEqual(f["severity"], "SEV2")
        self.assertIsNotNone(f["resolved_at"])


class StubIssuesClient:
    def __init__(self, issues):
        self._issues = issues

    def fetch_issues(self, owner, repo, state="all"):
        return self._issues


class IngestIssuesTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )

    def test_creates_incidents_and_skips_prs(self):
        result = ingest_issues(
            self.repo, [issue(1), issue(2, state="closed"), issue(3, pr=True)]
        )
        self.assertEqual(result.incidents_created, 2)
        self.assertEqual(result.skipped_pull_requests, 1)
        self.assertEqual(Incident.objects.filter(repository=self.repo).count(), 2)

    def test_idempotent_reingest_updates(self):
        ingest_issues(self.repo, [issue(1, title="old")])
        result = ingest_issues(self.repo, [issue(1, title="new")])
        self.assertEqual(result.incidents_updated, 1)
        self.assertEqual(Incident.objects.count(), 1)
        self.assertEqual(Incident.objects.get().title, "new")

    def test_ingest_repository_issues_uses_client(self):
        client = StubIssuesClient([issue(7, state="closed")])
        result = ingest_repository_issues(self.repo, client=client)
        self.assertEqual(result.incidents_created, 1)
        inc = Incident.objects.get(external_id="7")
        self.assertEqual(inc.status, IncidentStatus.RESOLVED)


class SingleIssueStub:
    def __init__(self, by_number):
        self.by_number = by_number

    def fetch_issue(self, owner, repo, number):
        return self.by_number.get(number)


class SeedFromFixRefsTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        from hictx.graph.models import ChangeKind, CodeChange
        CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40, title="fix bug", body="fixes #5, closes #6",
        )

    def test_seeds_referenced_issues_and_skips_prs(self):
        from hictx.graph.services.github_issues import seed_incidents_from_fix_refs
        client = SingleIssueStub({
            "5": issue(5, state="closed"),   # a real issue
            "6": issue(6, pr=True),          # a PR — must be skipped
        })
        result = seed_incidents_from_fix_refs(self.repo, client=client)
        self.assertEqual(result.incidents_created, 1)
        self.assertEqual(result.skipped_pull_requests, 1)
        self.assertTrue(Incident.objects.filter(external_id="5").exists())
        self.assertFalse(Incident.objects.filter(external_id="6").exists())

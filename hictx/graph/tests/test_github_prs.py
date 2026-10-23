from django.test import TestCase

from hictx.graph.models import ChangeKind, CodeChange, Provider, Repository
from hictx.graph.services.github_prs import (
    ingest_pull_requests,
    map_pr_to_change_fields,
)


def pr(number, title="t", body="b", merge_sha="abc123", merged=True):
    return {
        "number": number, "title": title, "body": body,
        "user": {"login": "dana"},
        "html_url": f"https://github.com/acme/payments/pull/{number}",
        "created_at": "2026-01-01T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        "merge_commit_sha": merge_sha,
    }


class StubPRsClient:
    def __init__(self, pulls):
        self._pulls = pulls

    def fetch_pulls(self, owner, repo, state="all"):
        return self._pulls


class MapPRTests(TestCase):
    def test_fields_include_body_and_merge_sha(self):
        f = map_pr_to_change_fields(pr(5, body="fixes #91", merge_sha="deadbeef"))
        self.assertEqual(f["body"], "fixes #91")
        self.assertEqual(f["merge_commit_sha"], "deadbeef")
        self.assertEqual(f["author_name"], "dana")
        self.assertIsNotNone(f["merged_at"])

    def test_unmerged_pr_has_no_merge_fields(self):
        f = map_pr_to_change_fields(pr(5, merge_sha=None, merged=False))
        self.assertIsNone(f["merged_at"])
        self.assertEqual(f["merge_commit_sha"], "")


class IngestPRsTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )

    def test_creates_pull_request_changes(self):
        client = StubPRsClient([pr(1, merge_sha="s1"), pr(2, merge_sha="s2")])
        result = ingest_pull_requests(self.repo, client=client)
        self.assertEqual(result.prs_created, 2)
        self.assertEqual(result.merged_with_sha, 2)
        pr1 = CodeChange.objects.get(kind=ChangeKind.PULL_REQUEST, identifier="1")
        self.assertEqual(pr1.merge_commit_sha, "s1")

    def test_idempotent_reingest_updates(self):
        ingest_pull_requests(self.repo, client=StubPRsClient([pr(1, title="old")]))
        result = ingest_pull_requests(self.repo, client=StubPRsClient([pr(1, title="new")]))
        self.assertEqual(result.prs_updated, 1)
        self.assertEqual(
            CodeChange.objects.filter(kind=ChangeKind.PULL_REQUEST).count(), 1
        )
        self.assertEqual(
            CodeChange.objects.get(kind=ChangeKind.PULL_REQUEST, identifier="1").title,
            "new",
        )

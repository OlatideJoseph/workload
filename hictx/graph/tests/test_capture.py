from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from hictx.graph.models import (
    ChangeKind,
    CausedByEdge,
    CodeChange,
    Decision,
    ImplementsEdge,
    Incident,
    Provider,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
    RootCause,
)
from hictx.graph.services.capture import capture_pr_rationale


class CaptureServiceTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )

    def test_creates_human_decision_and_confirmed_edge(self):
        result = capture_pr_rationale(
            repository=self.repo,
            change_identifier="482",
            why="Stripe 5xxes on webhook delivery, so we retry with backoff.",
        )
        self.assertTrue(result.created_decision)
        self.assertEqual(result.decision.rationale.startswith("Stripe 5xxes"), True)
        edge = result.edge
        self.assertEqual(edge.source, ProvenanceSource.HUMAN)
        self.assertEqual(edge.status, ProvenanceStatus.CONFIRMED)
        self.assertEqual(edge.confidence, 1.0)
        self.assertTrue(edge.evidence["captured_at_pr_open"])

    def test_upgrades_existing_inferred_edge(self):
        # The decision linker had already guessed this link.
        change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="x",
        )
        decision = Decision.objects.create(
            repository=self.repo, key="WHY-pull_request-482", title="t",
        )
        ImplementsEdge.objects.create(
            code_change=change, decision=decision,
            source=ProvenanceSource.HEURISTIC, confidence=0.65,
            status=ProvenanceStatus.INFERRED,
        )

        result = capture_pr_rationale(
            repository=self.repo, change_identifier="482",
            why="Because ADR-7.",
        )

        self.assertFalse(result.created_decision)  # reused the same key
        self.assertEqual(ImplementsEdge.objects.count(), 1)  # no duplicate
        self.assertEqual(result.edge.source, ProvenanceSource.HUMAN)
        self.assertEqual(result.edge.status, ProvenanceStatus.CONFIRMED)


class ReviewApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            external_id="91", title="Dup charges",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.COMMIT, identifier="abc123", title="c",
        )
        self.edge = CausedByEdge.objects.create(
            incident=self.incident, code_change=self.change,
            source=ProvenanceSource.HEURISTIC, confidence=0.6,
            status=ProvenanceStatus.INFERRED,
        )
        self.rc = RootCause.objects.create(
            incident=self.incident, summary="not idempotent",
            source=ProvenanceSource.LLM, status=ProvenanceStatus.INFERRED,
            confidence=0.7,
        )

    def test_confirm_edge_sets_status_and_reviewer(self):
        user = get_user_model().objects.create_user("reviewer", password="x")
        self.client.force_authenticate(user)
        resp = self.client.post(f"/api/caused-by-edges/{self.edge.pk}/confirm/")
        self.assertEqual(resp.status_code, 200)
        self.edge.refresh_from_db()
        self.assertEqual(self.edge.status, ProvenanceStatus.CONFIRMED)
        self.assertEqual(self.edge.reviewed_by, user)
        self.assertIsNotNone(self.edge.reviewed_at)

    def test_reject_root_cause(self):
        resp = self.client.post(f"/api/root-causes/{self.rc.pk}/reject/")
        self.assertEqual(resp.status_code, 200)
        self.rc.refresh_from_db()
        self.assertEqual(self.rc.status, ProvenanceStatus.REJECTED)

    def test_review_queue_lists_only_inferred(self):
        self.edge.confirm()  # drops out of the queue
        resp = self.client.get("/api/review-queue/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["caused_by_edges"]), 0)
        self.assertEqual(len(body["root_causes"]), 1)

    def test_capture_endpoint_creates_context(self):
        resp = self.client.post(
            "/api/capture/pr-why/",
            {
                "repository": "acme/payments",
                "change_identifier": "482",
                "why": "Retry webhooks per ADR-7.",
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["edge"]["status"], ProvenanceStatus.CONFIRMED)
        self.assertTrue(Decision.objects.filter(repository=self.repo).exists())

    def test_capture_unknown_repo_is_400(self):
        resp = self.client.post(
            "/api/capture/pr-why/",
            {"repository": "nope/nope", "change_identifier": "1", "why": "x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

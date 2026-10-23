from django.test import TestCase

from hictx.graph.models import (
    ChangeKind,
    CodeChange,
    Decision,
    DecisionStatus,
    ImplementsEdge,
    Provider,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services.decision_linker import DecisionLinker


class DecisionLinkerTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB,
            full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            status=DecisionStatus.ACCEPTED,
        )

    def _change(self, title="", body="", identifier="1"):
        return CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier=identifier,
            title=title, body=body,
        )

    def test_implements_verb_scores_higher(self):
        self._change(title="Add retry (implements ADR-7)", identifier="10")
        DecisionLinker(self.repo).run()
        edge = ImplementsEdge.objects.get(decision=self.decision)
        self.assertEqual(edge.confidence, 0.85)
        self.assertEqual(edge.status, ProvenanceStatus.INFERRED)
        self.assertEqual(edge.evidence["matched_key"], "ADR-7")

    def test_bare_mention_scores_lower(self):
        self._change(body="see ADR-7 for background", identifier="11")
        DecisionLinker(self.repo).run()
        self.assertEqual(
            ImplementsEdge.objects.get(decision=self.decision).confidence, 0.65
        )

    def test_unknown_decision_is_skipped(self):
        self._change(title="implements ADR-99", identifier="12")
        result = DecisionLinker(self.repo).run()
        self.assertEqual(ImplementsEdge.objects.count(), 0)
        self.assertTrue(any("ADR-99" in s for s in result.skipped))

    def test_idempotent_and_preserves_human_verdict(self):
        self._change(title="implements ADR-7", identifier="13")
        DecisionLinker(self.repo).run()
        edge = ImplementsEdge.objects.get(decision=self.decision)
        edge.status = ProvenanceStatus.REJECTED
        edge.confidence = 0.1
        edge.save()

        second = DecisionLinker(self.repo).run()

        self.assertEqual(second.edges_created, 0)
        edge.refresh_from_db()
        self.assertEqual(edge.status, ProvenanceStatus.REJECTED)
        self.assertEqual(edge.confidence, 0.1)
        self.assertEqual(ImplementsEdge.objects.count(), 1)

from django.test import TestCase

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
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
    RootCause,
)
from hictx.graph.services.context_test import run_context_test
from hictx.graph.services.llm import StubTaskRunner
from hictx.graph.services.retrieval import render_context, retrieve_entity_subgraph


class RetrievalTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FUNCTION,
            path="payments/webhooks.py", name="handle_charge_succeeded",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="retry",
        )
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.entity)

        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            rationale="Stripe 5xxes; retry with backoff.",
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            external_id="91", title="Duplicate charges", severity="SEV2",
        )

    def _implements(self, status, confidence):
        return ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            source=ProvenanceSource.HEURISTIC, confidence=confidence, status=status,
        )

    def _caused(self, status, confidence):
        return CausedByEdge.objects.create(
            incident=self.incident, code_change=self.change,
            source=ProvenanceSource.HEURISTIC, confidence=confidence, status=status,
        )

    def test_confirmed_edges_always_included(self):
        self._implements(ProvenanceStatus.CONFIRMED, 0.1)
        sub = retrieve_entity_subgraph(self.entity, include_inferred=False)
        self.assertEqual(len(sub.decisions), 1)

    def test_rejected_edges_never_included(self):
        self._implements(ProvenanceStatus.REJECTED, 1.0)
        sub = retrieve_entity_subgraph(self.entity, include_inferred=True, min_confidence=0)
        self.assertEqual(len(sub.decisions), 0)

    def test_inferred_filtered_by_confidence(self):
        self._caused(ProvenanceStatus.INFERRED, 0.4)
        # Below threshold with trusted-only:
        self.assertEqual(
            len(retrieve_entity_subgraph(self.entity, include_inferred=False).incidents), 0
        )
        # Included when we allow high-confidence inferred at/under its score:
        self.assertEqual(
            len(retrieve_entity_subgraph(
                self.entity, include_inferred=True, min_confidence=0.3
            ).incidents), 1
        )

    def test_root_causes_filtered_too(self):
        self._caused(ProvenanceStatus.CONFIRMED, 1.0)
        RootCause.objects.create(
            incident=self.incident, summary="good", source=ProvenanceSource.LLM,
            status=ProvenanceStatus.CONFIRMED, confidence=0.9,
        )
        RootCause.objects.create(
            incident=self.incident, summary="rejected-noise", source=ProvenanceSource.LLM,
            status=ProvenanceStatus.REJECTED, confidence=0.9,
        )
        sub = retrieve_entity_subgraph(self.entity)
        [inc] = sub.incidents
        summaries = [rc.summary for rc in inc.root_causes]
        self.assertEqual(summaries, ["good"])

    def test_render_context_mentions_decision_and_incident(self):
        self._implements(ProvenanceStatus.CONFIRMED, 1.0)
        self._caused(ProvenanceStatus.CONFIRMED, 1.0)
        text = render_context(retrieve_entity_subgraph(self.entity))
        self.assertIn("ADR-7", text)
        self.assertIn("Duplicate charges", text)
        self.assertIn("CONNECTED CONTEXT", text)


class ContextTestHarnessTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FUNCTION,
            path="payments/webhooks.py", name="handle",
        )

    def test_with_variant_receives_context_and_without_does_not(self):
        change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="t",
        )
        AffectsEdge.objects.create(code_change=change, code_entity=self.entity)
        decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry", rationale="why",
        )
        ImplementsEdge.objects.create(
            code_change=change, decision=decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )

        result = run_context_test(
            self.entity, "def handle(): ...", task="explain",
            runner=StubTaskRunner(),
        )
        self.assertTrue(result.had_context)
        self.assertIn("had_context=True", result.with_context)
        self.assertIn("had_context=False", result.without_context)

    def test_empty_subgraph_reports_no_context(self):
        result = run_context_test(
            self.entity, "def handle(): ...", runner=StubTaskRunner()
        )
        self.assertFalse(result.had_context)
        self.assertIn("had_context=False", result.with_context)

    def test_unknown_task_raises(self):
        with self.assertRaises(ValueError):
            run_context_test(self.entity, "x", task="nonsense", runner=StubTaskRunner())

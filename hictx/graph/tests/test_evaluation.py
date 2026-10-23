from django.test import SimpleTestCase, TestCase

from hictx.graph.models import (
    AffectsEdge,
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
    RootCause,
)
from hictx.graph.services.evaluation import (
    KeywordJudge,
    PreserveItem,
    evaluate_entity,
    must_preserve_items,
)
from hictx.graph.services.llm import EchoTaskRunner, StubTaskRunner
from hictx.graph.services.retrieval import retrieve_entity_subgraph


class KeywordJudgeTests(SimpleTestCase):
    def test_preserved_when_keywords_present(self):
        item = PreserveItem("d:1", "decision", "retry with backoff",
                            keywords=["retry", "backoff"])
        judge = KeywordJudge(min_overlap=0.5)
        self.assertTrue(judge.preserved(item, "we should retry on failure"))
        self.assertFalse(judge.preserved(item, "totally unrelated text"))

    def test_no_keywords_never_preserved(self):
        self.assertFalse(
            KeywordJudge().preserved(PreserveItem("x", "k", "", []), "anything")
        )


class MetricTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FUNCTION,
            path="payments/webhooks.py", name="handle",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="retry",
        )
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.entity)
        decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
            rationale="Stripe sometimes returns errors; retry with exponential backoff.",
        )
        ImplementsEdge.objects.create(
            code_change=self.change, decision=decision,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )
        incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB, external_id="91",
            title="Duplicate charges after retries",
        )
        from hictx.graph.models import CausedByEdge
        CausedByEdge.objects.create(
            incident=incident, code_change=self.change,
            status=ProvenanceStatus.CONFIRMED, confidence=1.0,
        )
        RootCause.objects.create(
            incident=incident, summary="Charge handler was not idempotent",
            status=ProvenanceStatus.CONFIRMED, confidence=0.9,
        )

    def test_must_preserve_items_covers_decision_incident_rootcause(self):
        items = must_preserve_items(retrieve_entity_subgraph(self.entity))
        kinds = {it.kind for it in items}
        self.assertEqual(kinds, {"decision", "incident", "root_cause"})

    def test_context_run_preserves_more_than_context_free(self):
        # EchoTaskRunner echoes the prompt: the with-context prompt contains the
        # context items, the without-context prompt does not -> positive delta.
        ev = evaluate_entity(
            self.entity, "def handle(): ...",
            runner=EchoTaskRunner(), judge=KeywordJudge(min_overlap=0.4),
        )
        self.assertGreater(ev.preserved_with, ev.preserved_without)
        self.assertGreater(ev.delta, 0)

    def test_entity_without_context_returns_none(self):
        lonely = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="unrelated.py",
        )
        ev = evaluate_entity(lonely, "x", runner=StubTaskRunner(), judge=KeywordJudge())
        self.assertIsNone(ev)

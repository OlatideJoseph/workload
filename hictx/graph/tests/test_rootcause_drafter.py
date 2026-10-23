from django.test import SimpleTestCase, TestCase

from hictx.graph.models import (
    ChangeKind,
    CodeChange,
    Incident,
    Provider,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
    RootCause,
)
from hictx.graph.services import llm
from hictx.graph.services.llm import RootCauseDraft, StubLLMClient
from hictx.graph.services.rootcause_drafter import RootCauseDrafter


class ParseDraftsTests(SimpleTestCase):
    def test_plain_json_array(self):
        text = '[{"summary": "s", "category": "c", "detail": "d", "confidence": 0.8}]'
        [d] = llm.parse_drafts(text)
        self.assertEqual((d.summary, d.category, d.confidence), ("s", "c", 0.8))

    def test_json_wrapped_in_prose_or_fence(self):
        text = "Here you go:\n```json\n[{\"summary\": \"x\", \"confidence\": 1}]\n```"
        [d] = llm.parse_drafts(text)
        self.assertEqual(d.summary, "x")

    def test_confidence_clamped_and_defaults(self):
        text = '[{"summary": "a", "confidence": 5}, {"summary": "b"}]'
        a, b = llm.parse_drafts(text)
        self.assertEqual(a.confidence, 1.0)   # clamped from 5
        self.assertEqual(b.confidence, 0.5)   # default

    def test_items_without_summary_skipped(self):
        self.assertEqual(llm.parse_drafts('[{"category": "c"}]'), [])

    def test_garbage_returns_empty(self):
        self.assertEqual(llm.parse_drafts("not json at all"), [])


class RootCauseDrafterTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB,
            full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            external_id="91", title="Duplicate charges",
        )

    def _drafter(self, drafts=None):
        return RootCauseDrafter(self.repo, llm_client=StubLLMClient(drafts))

    def test_creates_inferred_llm_root_cause(self):
        drafts = [RootCauseDraft(summary="Not idempotent", category="idempotency",
                                 detail="d", confidence=0.7)]
        result = self._drafter(drafts).run()

        self.assertEqual(result.root_causes_created, 1)
        rc = RootCause.objects.get(incident=self.incident)
        self.assertEqual(rc.source, ProvenanceSource.LLM)
        self.assertEqual(rc.status, ProvenanceStatus.INFERRED)
        self.assertEqual(rc.confidence, 0.7)
        self.assertEqual(rc.evidence["inputs"], ["incident"])

    def test_skips_incident_that_already_has_drafts(self):
        self._drafter().run()
        second = self._drafter().run()
        self.assertEqual(second.incidents_drafted, 0)
        self.assertTrue(any("already has LLM drafts" in s for s in second.skipped))
        self.assertEqual(RootCause.objects.filter(incident=self.incident).count(), 1)

    def test_force_redrafts_but_preserves_confirmed(self):
        self._drafter([RootCauseDraft(summary="old-inferred")]).run()
        # Human confirms a separate draft that must survive a re-draft.
        RootCause.objects.create(
            incident=self.incident, summary="human-confirmed",
            source=ProvenanceSource.LLM, status=ProvenanceStatus.CONFIRMED,
            confidence=0.9,
        )

        self._drafter([RootCauseDraft(summary="new-inferred")]).run(force=True)

        summaries = set(
            RootCause.objects.filter(incident=self.incident).values_list(
                "summary", flat=True
            )
        )
        self.assertIn("human-confirmed", summaries)   # verdict preserved
        self.assertIn("new-inferred", summaries)      # fresh draft added
        self.assertNotIn("old-inferred", summaries)   # stale inferred cleared

    def test_includes_linked_change_in_evidence(self):
        change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="fix",
        )
        self.incident.caused_by.add(change)  # through CausedByEdge

        self._drafter([RootCauseDraft(summary="s")]).run()

        rc = RootCause.objects.get(incident=self.incident)
        self.assertIn("change:482", rc.evidence["inputs"])

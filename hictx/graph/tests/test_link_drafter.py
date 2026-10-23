from django.test import SimpleTestCase, TestCase

from hictx.graph.models import (
    ChangeKind,
    CodeChange,
    Decision,
    ImplementsEdge,
    Provider,
    ProvenanceSource,
    ProvenanceStatus,
    Repository,
)
from hictx.graph.services.link_drafter import (
    DecisionSummary,
    KeywordLinkDraftClient,
    LLMLinkDraftClient,
    draft_implements_links,
)


def summary(key, title, rationale=""):
    from hictx.graph.services.link_drafter import _keywords
    return DecisionSummary(key=key, title=title, rationale=rationale,
                           keywords=_keywords(f"{title} {rationale}"))


class KeywordClientTests(SimpleTestCase):
    def setUp(self):
        self.decisions = [
            summary("ADR-19", "Service credentials projection",
                    "Project service bindings as a secret reference."),
            summary("ADR-4", "Structuring Kubernetes resource labels"),
        ]

    def test_proposes_on_topic_match(self):
        links = KeywordLinkDraftClient().propose_links(
            "Switch to projected service credentials secret", "", self.decisions
        )
        self.assertEqual([l.decision_key for l in links], ["ADR-19"])
        self.assertLessEqual(links[0].confidence, 0.7)

    def test_no_match_no_proposal(self):
        links = KeywordLinkDraftClient().propose_links(
            "Bump go version in CI", "", self.decisions
        )
        self.assertEqual(links, [])


class FakeRunner:
    def __init__(self, text):
        self.text = text

    def run(self, system, user):
        return self.text


class LLMClientTests(SimpleTestCase):
    def test_parses_json_and_drops_unknown_keys(self):
        decisions = [summary("ADR-7", "Retry webhooks")]
        runner = FakeRunner(
            '[{"decision_key":"ADR-7","confidence":0.8,"reason":"adds retry"},'
            '{"decision_key":"ADR-99","confidence":0.9,"reason":"nope"}]'
        )
        links = LLMLinkDraftClient(runner).propose_links("add retry", "", decisions)
        self.assertEqual([l.decision_key for l in links], ["ADR-7"])
        self.assertEqual(links[0].confidence, 0.8)


class DraftServiceTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/pay",
            url="https://github.com/acme/pay",
        )
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-19", title="Service credentials projection",
            rationale="Project service bindings as a secret reference.",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB, kind=ChangeKind.COMMIT,
            identifier="a" * 40,
            title="Switch to projected service credentials secret",
            body="Implements the projection of service bindings.",
        )

    def test_drafts_inferred_llm_edge(self):
        result = draft_implements_links(self.repo, client=KeywordLinkDraftClient())
        self.assertEqual(result.links_drafted, 1)
        edge = ImplementsEdge.objects.get(code_change=self.change, decision=self.decision)
        self.assertEqual(edge.source, ProvenanceSource.LLM)
        self.assertEqual(edge.status, ProvenanceStatus.INFERRED)
        self.assertIn("drafted_by", edge.evidence)

    def test_does_not_overwrite_human_verdict(self):
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.CONFIRMED, source=ProvenanceSource.HUMAN,
            confidence=1.0,
        )
        result = draft_implements_links(self.repo, client=KeywordLinkDraftClient())
        self.assertEqual(result.skipped_human, 1)
        self.assertEqual(result.links_drafted, 0)
        edge = ImplementsEdge.objects.get(code_change=self.change, decision=self.decision)
        self.assertEqual(edge.status, ProvenanceStatus.CONFIRMED)  # untouched

    def test_does_not_downgrade_heuristic_edge(self):
        ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision,
            status=ProvenanceStatus.INFERRED, source=ProvenanceSource.HEURISTIC,
            confidence=0.9,
        )
        result = draft_implements_links(self.repo, client=KeywordLinkDraftClient())
        self.assertEqual(result.links_drafted, 0)
        self.assertEqual(result.links_refreshed, 0)
        edge = ImplementsEdge.objects.get(code_change=self.change, decision=self.decision)
        self.assertEqual(edge.source, ProvenanceSource.HEURISTIC)  # preserved
        self.assertEqual(edge.confidence, 0.9)

    def test_no_decisions_note(self):
        Decision.objects.all().delete()
        result = draft_implements_links(self.repo, client=KeywordLinkDraftClient())
        self.assertEqual(result.links_drafted, 0)
        self.assertTrue(result.notes)

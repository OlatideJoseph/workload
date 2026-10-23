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
    ProvenanceStatus,
    Repository,
    RootCause,
)
from hictx.graph.services.graph_view import repository_graph


class RepositoryGraphTests(TestCase):
    def setUp(self):
        self.repo = Repository.objects.create(
            provider=Provider.GITHUB, full_name="acme/payments",
            url="https://github.com/acme/payments",
        )
        self.change = CodeChange.objects.create(
            repository=self.repo, provider=Provider.GITHUB,
            kind=ChangeKind.PULL_REQUEST, identifier="482", title="retry",
        )
        self.entity = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="webhooks.py",
        )
        AffectsEdge.objects.create(code_change=self.change, code_entity=self.entity)
        self.decision = Decision.objects.create(
            repository=self.repo, key="ADR-7", title="Retry webhooks",
        )
        self.incident = Incident.objects.create(
            repository=self.repo, provider=Provider.GITHUB, external_id="91",
            title="Dup charges",
        )

    def _implements(self, status, conf=0.9):
        return ImplementsEdge.objects.create(
            code_change=self.change, decision=self.decision, status=status, confidence=conf
        )

    def _caused(self, status, conf=0.6):
        return CausedByEdge.objects.create(
            incident=self.incident, code_change=self.change, status=status, confidence=conf
        )

    def test_confirmed_graph_has_all_node_and_edge_types(self):
        self._implements(ProvenanceStatus.CONFIRMED)
        self._caused(ProvenanceStatus.CONFIRMED)
        RootCause.objects.create(
            incident=self.incident, summary="not idempotent",
            status=ProvenanceStatus.CONFIRMED, confidence=0.8,
        )
        g = repository_graph(self.repo, include_inferred=False)
        types = {n["type"] for n in g["nodes"]}
        self.assertEqual(types, {"incident", "change", "decision", "root_cause", "entity"})
        edge_types = {e["type"] for e in g["edges"]}
        self.assertEqual(edge_types, {"caused_by", "implements", "explains", "affects"})

    def test_inferred_excluded_when_confirmed_only(self):
        self._caused(ProvenanceStatus.INFERRED)
        confirmed = repository_graph(self.repo, include_inferred=False)
        self.assertEqual(confirmed["edges"], [])
        self.assertEqual(confirmed["nodes"], [])
        with_inferred = repository_graph(self.repo, include_inferred=True, min_confidence=0.0)
        self.assertTrue(any(e["type"] == "caused_by" for e in with_inferred["edges"]))

    def test_rejected_never_included(self):
        self._caused(ProvenanceStatus.REJECTED)
        g = repository_graph(self.repo, include_inferred=True, min_confidence=0.0)
        self.assertEqual(g["edges"], [])

    def test_unlinked_files_are_not_in_graph(self):
        # A change/file with no incident/decision link should not appear.
        self._caused(ProvenanceStatus.CONFIRMED)
        lonely = CodeEntity.objects.create(
            repository=self.repo, kind=EntityKind.FILE, path="unrelated.py",
        )
        g = repository_graph(self.repo, include_inferred=False)
        paths = {n["label"] for n in g["nodes"] if n["type"] == "entity"}
        self.assertIn("webhooks.py", paths)
        self.assertNotIn("unrelated.py", paths)

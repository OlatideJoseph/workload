"""
Phase 4 retrieval: pull a code entity's connected subgraph.

For a file/module/function, walk:
    CodeEntity <-affects- CodeChange -implements-> Decision
                          CodeChange -caused-> Incident -> RootCause

Inferred edges carry provenance, so retrieval FILTERS them (CLAUDE.md: "filter to
only trusted edges when feeding context to a migration"). A rejected edge is never
included; a confirmed edge always is; an inferred edge is included only when
`include_inferred` and its confidence clears `min_confidence`. Factual `affects`
edges have no provenance and are always included.

The result renders two ways: `to_dict()` for the API, `render_context()` for the
text block fed to a migration/explanation task.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import (
    AffectsEdge,
    CausedByEdge,
    CodeEntity,
    ImplementsEdge,
    ProvenanceStatus,
)


def edge_allowed(edge, include_inferred: bool, min_confidence: float) -> bool:
    if edge.status == ProvenanceStatus.REJECTED:
        return False
    if edge.status == ProvenanceStatus.CONFIRMED:
        return True
    # inferred
    return include_inferred and edge.confidence >= min_confidence


@dataclass
class DecisionHit:
    decision: object
    edge: object


@dataclass
class IncidentHit:
    incident: object
    edge: object
    root_causes: list = field(default_factory=list)


@dataclass
class ChangeHit:
    change: object
    change_type: str


@dataclass
class EntitySubgraph:
    entity: CodeEntity
    changes: list[ChangeHit] = field(default_factory=list)
    decisions: list[DecisionHit] = field(default_factory=list)
    incidents: list[IncidentHit] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.decisions or self.incidents)

    def to_dict(self) -> dict:
        return {
            "entity": {
                "id": self.entity.pk,
                "kind": self.entity.kind,
                "path": self.entity.path,
                "name": self.entity.name,
            },
            "changes": [
                {
                    "identifier": h.change.identifier,
                    "kind": h.change.kind,
                    "title": h.change.title,
                    "change_type": h.change_type,
                }
                for h in self.changes
            ],
            "decisions": [
                {
                    "key": h.decision.key,
                    "title": h.decision.title,
                    "status": h.decision.status,
                    "rationale": h.decision.rationale,
                    "link": {"source": h.edge.source, "confidence": h.edge.confidence,
                             "status": h.edge.status},
                }
                for h in self.decisions
            ],
            "incidents": [
                {
                    "external_id": h.incident.external_id,
                    "title": h.incident.title,
                    "severity": h.incident.severity,
                    "link": {"source": h.edge.source, "confidence": h.edge.confidence,
                             "status": h.edge.status},
                    "root_causes": [
                        {"summary": rc.summary, "category": rc.category,
                         "source": rc.source, "confidence": rc.confidence,
                         "status": rc.status}
                        for rc in h.root_causes
                    ],
                }
                for h in self.incidents
            ],
        }


def retrieve_entity_subgraph(
    entity: CodeEntity,
    include_inferred: bool = True,
    min_confidence: float = 0.5,
) -> EntitySubgraph:
    subgraph = EntitySubgraph(entity=entity)

    affects = AffectsEdge.objects.filter(code_entity=entity).select_related("code_change")
    changes = {}
    for ae in affects:
        subgraph.changes.append(ChangeHit(change=ae.code_change, change_type=ae.change_type))
        changes[ae.code_change.pk] = ae.code_change

    if not changes:
        return subgraph

    seen_decisions: set = set()
    for ie in ImplementsEdge.objects.filter(
        code_change_id__in=changes
    ).select_related("decision"):
        if not edge_allowed(ie, include_inferred, min_confidence):
            continue
        if ie.decision_id in seen_decisions:
            continue
        seen_decisions.add(ie.decision_id)
        subgraph.decisions.append(DecisionHit(decision=ie.decision, edge=ie))

    seen_incidents: set = set()
    for ce in CausedByEdge.objects.filter(
        code_change_id__in=changes
    ).select_related("incident"):
        if not edge_allowed(ce, include_inferred, min_confidence):
            continue
        if ce.incident_id in seen_incidents:
            continue
        seen_incidents.add(ce.incident_id)
        # Root causes carry provenance too — apply the same trust filter.
        rcs = [
            rc
            for rc in ce.incident.root_causes.all()
            if edge_allowed(rc, include_inferred, min_confidence)
        ]
        subgraph.incidents.append(
            IncidentHit(incident=ce.incident, edge=ce, root_causes=rcs)
        )

    return subgraph


def render_context(subgraph: EntitySubgraph) -> str:
    """Render the subgraph as the CONNECTED CONTEXT block fed to an AI task."""
    e = subgraph.entity
    target = f"{e.path}::{e.name}" if e.name else e.path
    lines = [f"CONNECTED CONTEXT for {target}", ""]

    if subgraph.decisions:
        lines.append("Decisions behind changes to this code (preserve this intent):")
        for h in subgraph.decisions:
            lines.append(f"  - [{h.decision.key} · {h.edge.status}] {h.decision.title}")
            if h.decision.rationale:
                lines.append(f"      rationale: {h.decision.rationale}")
        lines.append("")

    if subgraph.incidents:
        lines.append("Past incidents caused by changes to this code (do NOT reintroduce):")
        for h in subgraph.incidents:
            sev = f" [{h.incident.severity}]" if h.incident.severity else ""
            lines.append(f"  - {h.incident.title}{sev}")
            for rc in h.root_causes:
                lines.append(
                    f"      root cause ({rc.source} {rc.confidence:.2f}): {rc.summary}"
                )
        lines.append("")

    if subgraph.is_empty():
        lines.append("(No trusted context found for this entity.)")

    return "\n".join(lines).rstrip() + "\n"

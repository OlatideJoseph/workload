"""
Graph-view assembly for the frontend preview.

Returns the *connected knowledge core* of a repository — not every commit/file,
but the nodes that actually carry "why": incidents, the changes that caused them,
the decisions those changes implement, root causes, and the files those changes
touch. Inferred edges are trust-filtered exactly like retrieval, and each edge
reports its provenance so the UI can render confirmed vs inferred differently.

Shape: {"nodes": [{id, type, label, ...}], "edges": [{source, target, type,
status, confidence}]}. Node ids are "<type>:<pk>" so they're stable and unique.
"""

from __future__ import annotations

from ..models import AffectsEdge, CausedByEdge, ImplementsEdge, RootCause
from .retrieval import edge_allowed


def repository_graph(
    repository, include_inferred: bool = True, min_confidence: float = 0.5
) -> dict:
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    linked_change_pks: set[int] = set()

    def add_node(node_id, node_type, label, **meta):
        if node_id not in nodes:
            nodes[node_id] = {"id": node_id, "type": node_type,
                              "label": label or node_id, **meta}

    # incident --caused_by--> change
    for e in (
        CausedByEdge.objects.filter(incident__repository=repository)
        .select_related("incident", "code_change")
    ):
        if not edge_allowed(e, include_inferred, min_confidence):
            continue
        inc, ch = e.incident, e.code_change
        add_node(f"incident:{inc.pk}", "incident", inc.title,
                 external_id=inc.external_id, status=inc.status)
        add_node(f"change:{ch.pk}", "change", ch.title or ch.identifier,
                 identifier=ch.identifier[:12], kind=ch.kind)
        linked_change_pks.add(ch.pk)
        edges.append(_edge(f"incident:{inc.pk}", f"change:{ch.pk}",
                           "caused_by", e.status, e.confidence))

    # change --implements--> decision
    for e in (
        ImplementsEdge.objects.filter(decision__repository=repository)
        .select_related("code_change", "decision")
    ):
        if not edge_allowed(e, include_inferred, min_confidence):
            continue
        ch, d = e.code_change, e.decision
        add_node(f"change:{ch.pk}", "change", ch.title or ch.identifier,
                 identifier=ch.identifier[:12], kind=ch.kind)
        add_node(f"decision:{d.pk}", "decision", d.key, title=d.title)
        linked_change_pks.add(ch.pk)
        edges.append(_edge(f"change:{ch.pk}", f"decision:{d.pk}",
                           "implements", e.status, e.confidence))

    # root_cause --explains--> incident
    for rc in (
        RootCause.objects.filter(incident__repository=repository)
        .select_related("incident")
    ):
        if not edge_allowed(rc, include_inferred, min_confidence):
            continue
        inc = rc.incident
        add_node(f"incident:{inc.pk}", "incident", inc.title,
                 external_id=inc.external_id, status=inc.status)
        add_node(f"rootcause:{rc.pk}", "root_cause", rc.summary,
                 category=rc.category)
        edges.append(_edge(f"rootcause:{rc.pk}", f"incident:{inc.pk}",
                           "explains", rc.status, rc.confidence))

    # change --affects--> entity, only for changes already in the graph (keep it tight)
    if linked_change_pks:
        for ae in (
            AffectsEdge.objects.filter(code_change_id__in=linked_change_pks)
            .select_related("code_entity")
        ):
            ent = ae.code_entity
            add_node(f"entity:{ent.pk}", "entity", ent.path, name=ent.name)
            edges.append(_edge(f"change:{ae.code_change_id}", f"entity:{ent.pk}",
                               "affects", "factual", 1.0))

    return {"nodes": list(nodes.values()), "edges": edges}


def _edge(source, target, edge_type, status, confidence):
    return {"source": source, "target": target, "type": edge_type,
            "status": status, "confidence": confidence}

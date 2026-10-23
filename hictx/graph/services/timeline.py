"""
Repo story: the notable events of a repository, ordered in time.

This is the *temporal* view of the graph. The curation problem — you can't put
4000 commits on a timeline — is solved by the graph itself: the notable moments
are the connected ones. So the story is decisions (ADRs), incidents, and the
changes that implement a decision or caused/fixed an incident — exactly the nodes
the graph already surfaces — laid out by date.

Dates: a decision uses its parsed `decided_at` (falling back to the earliest
change that implements it); an incident uses `started_at`; a change uses
`authored_at`. Undated events sort to the end.
"""

from __future__ import annotations

from datetime import datetime, time, timezone as dt_tz

from ..models import AffectsEdge, CausedByEdge, CodeChange, ImplementsEdge


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _finalize(events: list[dict], limit: int) -> tuple[list[dict], dict]:
    """Sort events by date (undated last), strip the sort key, compute the span."""
    far = datetime.max.replace(tzinfo=dt_tz.utc)
    events.sort(key=lambda e: e["_sort"] or far)
    for e in events:
        e.pop("_sort", None)
    events = events[:limit]
    dated = [e for e in events if e["date"]]
    span = {
        "from": dated[0]["date"] if dated else None,
        "to": dated[-1]["date"] if dated else None,
    }
    return events, span


def repository_timeline(repository, limit: int = 200) -> dict:
    impl_edges = list(
        ImplementsEdge.objects.filter(decision__repository=repository)
        .select_related("code_change", "decision")
    )
    caused_edges = list(
        CausedByEdge.objects.filter(incident__repository=repository)
        .select_related("code_change", "incident")
    )

    # earliest implementing-change date per decision, for decisions with no date
    earliest_impl: dict[int, datetime] = {}
    for e in impl_edges:
        d = e.code_change.authored_at
        if d and (e.decision_id not in earliest_impl or d < earliest_impl[e.decision_id]):
            earliest_impl[e.decision_id] = d

    events: list[dict] = []

    for dec in repository.decisions.all():
        dt = None
        if dec.decided_at:
            dt = datetime.combine(dec.decided_at, time.min, tzinfo=dt_tz.utc)
        elif dec.pk in earliest_impl:
            dt = earliest_impl[dec.pk]
        events.append({
            "kind": "decision",
            "id": f"decision:{dec.pk}",
            "date": _iso(dt),
            "title": f"{dec.key} — {dec.title}",
            "subtitle": dec.get_status_display(),
            "detail": (dec.rationale or dec.context or "")[:280],
            "links": [],
            "_sort": dt,
        })

    for inc in repository.incidents.all():
        dt = inc.started_at or inc.resolved_at
        events.append({
            "kind": "incident",
            "id": f"incident:{inc.pk}",
            "date": _iso(dt),
            "title": inc.title,
            "subtitle": inc.severity or inc.get_status_display(),
            "detail": (inc.description or "")[:280],
            "links": [],
            "_sort": dt,
        })

    # changes connected to a decision or incident — the notable ones
    change_links: dict[int, list[str]] = {}
    for e in impl_edges:
        change_links.setdefault(e.code_change_id, []).append(e.decision.key)
    for e in caused_edges:
        change_links.setdefault(e.code_change_id, []).append(
            f"incident #{e.incident.external_id}"
        )
    for ch in CodeChange.objects.filter(id__in=set(change_links)):
        events.append({
            "kind": "change",
            "id": f"change:{ch.pk}",
            "date": _iso(ch.authored_at),
            "title": ch.title or ch.identifier,
            "subtitle": f"{ch.get_kind_display()} {ch.identifier[:10]}",
            "detail": (ch.body or "")[:280],
            "links": change_links[ch.pk],
            "_sort": ch.authored_at,
        })

    total = len(events)
    events, span = _finalize(events, limit)
    return {
        "repository": repository.full_name,
        "events": events,
        "span": span,
        "count": total,
    }


def file_timeline(entity, limit: int = 80) -> dict:
    """
    The story of a single file: the changes that touched it, the decisions those
    changes implement, and the incidents they caused — in time order.

    Scoped version of the repo timeline: it starts from the file's `affects` edges
    (its real change history) and pulls in the decisions/incidents those changes
    connect to, so a reader sees both WHAT changed and WHY.
    """
    affects = (
        AffectsEdge.objects.filter(code_entity=entity)
        .select_related("code_change")
    )
    change_by_id: dict[int, object] = {}
    change_type: dict[int, str] = {}
    for ae in affects:
        change_by_id[ae.code_change_id] = ae.code_change
        change_type[ae.code_change_id] = ae.change_type
    change_ids = set(change_by_id)

    impl = list(
        ImplementsEdge.objects.filter(code_change_id__in=change_ids)
        .select_related("decision")
    )
    caused = list(
        CausedByEdge.objects.filter(code_change_id__in=change_ids)
        .select_related("incident")
    )

    links: dict[int, list[str]] = {}
    decisions: dict[int, object] = {}
    incidents: dict[int, object] = {}
    earliest_impl: dict[int, datetime] = {}
    for e in impl:
        links.setdefault(e.code_change_id, []).append(e.decision.key)
        decisions[e.decision_id] = e.decision
        d = change_by_id[e.code_change_id].authored_at
        if d and (e.decision_id not in earliest_impl or d < earliest_impl[e.decision_id]):
            earliest_impl[e.decision_id] = d
    for e in caused:
        links.setdefault(e.code_change_id, []).append(f"incident #{e.incident.external_id}")
        incidents[e.incident_id] = e.incident

    events: list[dict] = []
    for cid, ch in change_by_id.items():
        events.append({
            "kind": "change",
            "id": f"change:{ch.pk}",
            "date": _iso(ch.authored_at),
            "title": ch.title or ch.identifier,
            "subtitle": f"{ch.get_kind_display()} · {change_type[cid]}",
            "detail": (ch.body or "")[:280],
            "links": links.get(cid, []),
            "_sort": ch.authored_at,
        })
    for dec in decisions.values():
        dt = None
        if dec.decided_at:
            dt = datetime.combine(dec.decided_at, time.min, tzinfo=dt_tz.utc)
        elif dec.pk in earliest_impl:
            dt = earliest_impl[dec.pk]
        events.append({
            "kind": "decision", "id": f"decision:{dec.pk}", "date": _iso(dt),
            "title": f"{dec.key} — {dec.title}", "subtitle": dec.get_status_display(),
            "detail": (dec.rationale or dec.context or "")[:280], "links": [], "_sort": dt,
        })
    for inc in incidents.values():
        dt = inc.started_at or inc.resolved_at
        events.append({
            "kind": "incident", "id": f"incident:{inc.pk}", "date": _iso(dt),
            "title": inc.title, "subtitle": inc.severity or inc.get_status_display(),
            "detail": (inc.description or "")[:280], "links": [], "_sort": dt,
        })

    # A file can have hundreds of routine edits; the STORY is its decisions,
    # incidents, and the changes that enacted them. Always keep those (so the span
    # stays real), then top up with the most recent routine edits for activity.
    core = [e for e in events if e["kind"] != "change" or e["links"]]
    routine = sorted(
        (e for e in events if e["kind"] == "change" and not e["links"]),
        key=lambda e: e["_sort"] or datetime.min.replace(tzinfo=dt_tz.utc),
        reverse=True,
    )
    budget = max(0, limit - len(core))
    selected = core + routine[:budget]
    events, span = _finalize(selected, limit)
    label = f"{entity.path}::{entity.name}" if entity.name else entity.path
    return {"entity": label, "events": events, "span": span, "count": len(events)}

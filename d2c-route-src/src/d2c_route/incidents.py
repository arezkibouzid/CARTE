"""C6: incidents become graph edits, then a local, stable repair.

Only convoys whose *remaining* schedule uses an edited road (or the convoy
that broke down) are replanned, from where they are at the incident time.
Roads of their current plan get a cost discount (``stability``), so the new
route deviates only where it must. Other convoys keep their plan and their
road reservations. ``mode="full"`` replans everything from scratch, as the
ablation baseline of the proposal (section 8.3).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .explain import hhmm
from .graph import edge_midpoint, edges_within, location_xy, point_distance, resolve, set_edge_attr, ukey
from .optimizer import EdgeCoster, Reservations, plan_convoy, schedule_route
from .schemas import ConstraintProgram, ConvoyPlan, Incident, IncidentType, MissionSpec, Plan, Route
from .vehicles import ConvoyProfile
from .verifier import diagnose, operator_message

DEFAULT_BREAKDOWN_DELAY_MIN = 30.0


@dataclass
class RepairReport:
    incident: Incident
    edited_edges: list[tuple[Any, Any]]
    affected: list[str]
    latency_s: float
    unchanged_share: float  # convoys whose remaining MSR is identical after repair
    messages: list[str] = field(default_factory=list)


def apply_incident(G: nx.DiGraph, inc: Incident) -> tuple[list[tuple[Any, Any]], str]:
    """Edit the graph in place; returns the edited directed edges and a summary."""
    if inc.type == IncidentType.BREAKDOWN:
        return [], f"breakdown of {inc.convoy_id or 'unknown convoy'}"
    if inc.location is None:
        if inc.type == IncidentType.WEATHER:
            edges = list(G.edges)
        else:
            raise ValueError(f"incident {inc.id} has no location")
    else:
        center = location_xy(G, inc.location)
        edges = edges_within(G, center, inc.radius_m)
        if inc.type == IncidentType.BRIDGE_DESTROYED:
            bridges = [e for e in edges if G.edges[e]["bridge"]]
            if not bridges:  # the report points near the bridge, take the closest one
                all_b = [e for e in G.edges if G.edges[e]["bridge"]]
                bridges = sorted(all_b, key=lambda e: point_distance(G, edge_midpoint(G, *e), center))[:1]
                if bridges:  # include the reverse direction of the same bridge
                    u, v = bridges[0]
                    bridges += [(v, u)] if G.has_edge(v, u) else []
            edges = bridges
        elif not edges:
            u, v = min(G.edges, key=lambda e: point_distance(G, edge_midpoint(G, *e), center))
            edges = [(u, v)] + ([(v, u)] if G.has_edge(v, u) else [])

    if inc.type in (IncidentType.BRIDGE_DESTROYED, IncidentType.ROAD_BLOCKED):
        for u, v in edges:
            set_edge_attr(G, u, v, closed=True)
        what = "closed"
    elif inc.type == IncidentType.THREAT_ZONE:
        center = location_xy(G, inc.location)
        for u, v in edges:
            d = point_distance(G, edge_midpoint(G, u, v), center)
            level = inc.severity * (1.0 - 0.5 * d / max(inc.radius_m, 1.0))
            set_edge_attr(G, u, v, threat=max(G[u][v]["threat"], level))
        what = f"threat up to {inc.severity:.1f}"
    else:  # weather
        for u, v in edges:
            set_edge_attr(G, u, v, weather=max(G[u][v]["weather"], inc.severity))
        what = f"weather degradation {inc.severity:.1f}"
    both = {e for u, v in edges for e in ((u, v), (v, u)) if G.has_edge(*e)}
    return sorted(both, key=str), f"{inc.type.value}: {len(both)} road segments {what}"


def _position(cp: ConvoyPlan, now: float) -> tuple[int, Any, float]:
    """(index of first edge still to drive, node to replan from, time there)."""
    if not cp.schedule or now <= cp.departure_min:
        start = cp.msr.nodes[0] if cp.msr else None
        return 0, start, max(now, cp.departure_min)
    for i, s in enumerate(cp.schedule):
        if s.t_in >= now:
            return i, s.u, s.t_in
    # on the last edge or arrived
    return len(cp.schedule), cp.msr.nodes[-1], max(now, cp.arrival_min or now)


def _remaining_keys(cp: ConvoyPlan, now: float) -> set[tuple[Any, Any]]:
    """Roads the convoy will still use (edges it is currently on count too)."""
    keys = {ukey(s.u, s.v) for s in cp.schedule if s.t_out >= now}
    if not cp.schedule and cp.msr:
        keys = {ukey(u, v) for u, v in cp.msr.edges()}
    for r in cp.asrs:
        keys |= {ukey(u, v) for u, v in r.edges()}  # an ASR hit also needs a new alternate
    return keys


def repair(
    G: nx.DiGraph,
    plan: Plan,
    mission: MissionSpec,
    program: ConstraintProgram,
    profiles: dict[str, ConvoyProfile],
    inc: Incident,
    stability: float = 0.3,
    mode: str = "local",
    costers: dict[str, EdgeCoster] | None = None,
) -> tuple[Plan, RepairReport]:
    t0 = time.perf_counter()
    now = inc.time_min
    edited, summary = apply_incident(G, inc)
    edited_keys = {ukey(u, v) for u, v in edited}
    costers = costers if costers is not None else {}
    for cid in list(costers):
        costers[cid].refresh(edited)

    convoys = {c.id: c for c in mission.convoys}
    order = sorted(mission.convoys, key=lambda c: (c.priority, c.earliest_departure_min, c.id))
    if mode == "full":
        affected = [c.id for c in order if (plan.convoys[c.id].arrival_min or 0) > now or plan.convoys[c.id].msr is None]
    else:
        affected = [
            c.id
            for c in order
            if (inc.type == IncidentType.BREAKDOWN and c.id == inc.convoy_id)
            or (plan.convoys[c.id].msr is None)
            or (_remaining_keys(plan.convoys[c.id], now) & edited_keys)
        ]

    new = Plan(mission_id=plan.mission_id, convoys=dict(plan.convoys), version=plan.version + 1, log=list(plan.log))
    reservations = Reservations.from_plan(plan, exclude=set(affected))
    msgs = [summary]
    for cid in affected:
        old = plan.convoys[cid]
        c = convoys[cid]
        idx, start, t_start = _position(old, now) if old.msr else (0, None, max(now, c.earliest_departure_min))
        if inc.type == IncidentType.BREAKDOWN and cid == inc.convoy_id:
            t_start += inc.duration_min or DEFAULT_BREAKDOWN_DELAY_MIN
        if cid not in costers:
            costers[cid] = EdgeCoster(G, program.convoys[cid], profiles[cid], program.unknown_penalty)
        coster = costers[cid]
        remaining_old = Route(nodes=old.msr.nodes[idx:], length_m=0, travel_time_min=0, cost=0) if old.msr else None
        if old.msr and len(remaining_old.nodes) < 2 and idx > 0:
            new.convoys[cid] = old  # already arrived
            continue
        cp = plan_convoy(
            G,
            c,
            program.convoys[cid],
            profiles[cid],
            reservations,
            program.unknown_penalty,
            start_node=start,
            start_time=t_start,
            previous=remaining_old if mode == "local" else None,
            stability=stability if mode == "local" else 0.0,
            coster=coster,
        )
        if old.msr and idx > 0 and cp.msr is not None:
            cp = _stitch(coster, old, idx, cp, program.convoys[cid].temporal)
        if cp.msr is None:
            blocking, _ = diagnose(G, coster, start if start is not None else resolve(G, c.origin), resolve(G, c.destination), {})
            cp.messages.append(operator_message(blocking))
        changed = old.msr is None or cp.msr is None or cp.msr.nodes != old.msr.nodes
        where = (G.nodes[start].get("name") or f"node {start}") if start is not None else str(c.origin)
        cp.messages.append(f"{inc.id} at {hhmm(now)}: {'rerouted' if changed else 'route kept'} from {where}")
        new.convoys[cid] = cp
        reservations.add(cp)
        msgs.append(f"{cid}: {'rerouted' if changed else 'route kept'}{' (' + cp.status + ')' if cp.status != 'ok' else ''}")

    unchanged = sum(
        1
        for cid in plan.convoys
        if plan.convoys[cid].msr is not None
        and new.convoys[cid].msr is not None
        and plan.convoys[cid].msr.nodes == new.convoys[cid].msr.nodes
    )
    new.log.append(f"v{new.version}: {inc.id} {summary}; replanned {affected or 'none'}")
    rep = RepairReport(
        incident=inc,
        edited_edges=edited,
        affected=affected,
        latency_s=time.perf_counter() - t0,
        unchanged_share=unchanged / max(1, len(plan.convoys)),
        messages=msgs,
    )
    return new, rep


def _stitch(coster: EdgeCoster, old: ConvoyPlan, idx: int, suffix: ConvoyPlan, temporal: dict) -> ConvoyPlan:
    """Keep the part already driven, append the repaired remainder."""
    nodes = old.msr.nodes[: idx + 1] + suffix.msr.nodes[1:]
    route = coster.route(nodes)
    sched = old.schedule[:idx] + suffix.schedule
    return suffix.model_copy(
        update={
            "msr": route,
            "departure_min": old.departure_min,
            "schedule": sched,
            "halts": [h for h in old.halts if h.t_start < (sched[idx].t_in if idx < len(sched) else 1e9)] + suffix.halts,
        }
    )


__all__ = ["apply_incident", "repair", "RepairReport", "schedule_route"]

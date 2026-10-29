"""S10: incident loop (methodology 5, S10).

For each incident report:

1. the report is parsed (``parsing``), with its source grading;
2. beliefs are updated (``belief``): closure probabilities, threat levels,
   weather, statuses; per-group road evaluations are refreshed;
3. if the change is the loss of one element covered by a briefed playbook, its
   response is applied (it was verified before departure);
4. otherwise the network is repaired: the design model (S5) on the remaining
   mission, from every column's current position, with the previous
   designations in the candidate pools, the unaffected groups' main routes
   fixed, and stability appended to the priority order (or first);
5. movement credits are re-solved (S7) with the past fixed and stability;
6. the new plan is verified by the rule checker (S8);
7. redundancy is restored: the design keeps NET.SURVIVE on the remaining
   routes; where a single point of failure is irreducible, the options are
   listed (reconnoitre by value of information, protect, request a
   gap-crossing asset, accept);
8. a FRAGO is written from the structured difference between the old and the
   new plan, only for the units affected.

A breakdown only re-times (the column stops; it blocks the segment unless it
can pull aside on a two-lane road). A reconnaissance result updates the map
and re-plans. A column caught on a segment whose lost element lies ahead turns
back to the decision point it came from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .anticipation import Playbook, ReconTask, value_of_information
from .belief import BeliefState, Report, report_probability
from .candidates import Route, generate_candidates, make_route, allowed_halt_nodes
from .corridors import CorridorGraph
from .criticality import Criticality, assess
from .design import Design, DesignProblem, solve
from .graph import edge_midpoint, edges_within, location_xy, point_distance, set_edge_attr, ukey
from .mission import MissionContext, build_context
from .orders import PlanDiff, UnitChange, write_frago
from .refine import Refinement, refine, route_story
from .requirements import Requirement
from .schedule import Credit, Schedule, can_pull_aside, hhmm, replan_tasks, schedule
from .schemas import Incident, IncidentType, LocationRef
from .verify import VerificationReport, verify

TURN_AROUND_MIN = 10.0
DEFAULT_BREAKDOWN_MIN = 30.0
CLOSURE_TYPES = (IncidentType.BRIDGE_DESTROYED, IncidentType.ROAD_BLOCKED)


@dataclass
class PlanState:
    """The current plan, as the incident loop sees it."""

    H: CorridorGraph
    ctx: MissionContext  # original context: units, groups and costers used by the movement credits
    requirements: list[Requirement]
    order: list[str]
    eps: dict[str, float]
    design: Design  # current designations (routes from the groups' current positions after a repair)
    crit: Criticality
    ref: Refinement
    sched: Schedule
    playbooks: list[Playbook] = field(default_factory=list)
    belief: BeliefState | None = None
    llm: Any = None
    plan_ctx: MissionContext | None = None  # context the current design was made in
    frago_no: int = 0
    stability_first: bool = False
    time_limit_s: float = 10.0
    use_cpsat: bool = True
    log: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.belief is None:
            g = next(iter(self.ctx.groups.values()), None)
            hl = g.active.param("SEG.STATUS", "half_life_min", 60.0) if g else 60.0
            cp = g.active.param("SEG.STATUS", "closed_p", 0.5) if g else 0.5
            sp = g.active.param("SEG.STATUS", "suspect_p", 0.2) if g else 0.2
            self.belief = BeliefState(self.H.G, hl, cp, sp)
        if self.plan_ctx is None:
            self.plan_ctx = self.ctx


@dataclass
class IncidentOutcome:
    incident: Incident
    mode: str  # playbook | repair | reschedule | recce | logged
    summary: str
    changed: list[tuple[Any, Any]]
    newly_closed: list[str]
    affected: list[str]
    playbook: str | None = None
    design: Design | None = None
    ref: Refinement | None = None
    sched: Schedule | None = None
    verification: VerificationReport | None = None
    restoration: list[str] = field(default_factory=list)
    recce: list[ReconTask] = field(default_factory=list)
    diff: PlanDiff | None = None
    frago: str = ""
    frago_source: str = ""
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Locating an incident on the map
# ---------------------------------------------------------------------------


def locate(H: CorridorGraph, inc: Incident) -> tuple[list[tuple[Any, Any]], list[str], tuple[float, float] | None]:
    """Directed road edges and critical elements an incident applies to (D2C-Route's rule, element-aware)."""
    G = H.G
    if inc.type == IncidentType.BREAKDOWN:
        return [], [], None
    if inc.location is None:
        return (list(G.edges), [], None) if inc.type == IncidentType.WEATHER else ([], [], None)
    center = location_xy(G, inc.location)
    if inc.type in (IncidentType.BRIDGE_DESTROYED, IncidentType.RECCE_RESULT) or (
            inc.type == IncidentType.CLEARED and inc.attrs.get("element")):
        kinds = ("bridge",) if inc.type == IncidentType.BRIDGE_DESTROYED else ("bridge", "tunnel", "ford", "ferry", "level_crossing", "defile")
        els = [e for e in H.elements.values() if e.kind in kinds]
        if not els:
            return [], [], center
        el = min(els, key=lambda e: point_distance(G, e.xy, center))
        edges = [x for u, v in el.edges for x in ((u, v), (v, u)) if G.has_edge(*x)]
        return edges, [el.id], center
    edges = edges_within(G, center, inc.radius_m)
    if not edges and inc.type in (IncidentType.ROAD_BLOCKED, IncidentType.CLEARED):
        u, v = min(G.edges, key=lambda e: point_distance(G, edge_midpoint(G, *e), center))
        edges = [(u, v)] + ([(v, u)] if G.has_edge(v, u) else [])
    keys = {ukey(u, v) for u, v in edges}
    hit = sorted({H.edge_elements[k] for k in keys if k in H.edge_elements})
    return sorted(set(edges), key=str), hit, center


# ---------------------------------------------------------------------------
# Positions of the columns
# ---------------------------------------------------------------------------


@dataclass
class Position:
    unit: str
    state: str  # waiting (not departed) | moving | arrived
    node: Any  # restart node
    time: float  # restart time
    leg: int | None = None  # index of the current leg (moving)


def positions(H: CorridorGraph, sched: Schedule, now: float) -> dict[str, Position]:
    out = {}
    for uid, c in sched.credits.items():
        if not c.legs:
            continue
        if c.arrive <= now:
            out[uid] = Position(uid, "arrived", c.legs[-1].end, c.arrive)
        elif c.depart >= now:
            out[uid] = Position(uid, "waiting", c.legs[0].start, max(now, c.earliest))
        else:
            i = max(j for j, l in enumerate(c.legs) if l.t_in <= now)
            out[uid] = Position(uid, "moving", c.legs[i].end, max(c.legs[i].t_out, now), i)
    return out


def _ahead_on_leg(H: CorridorGraph, c: Credit, i: int, now: float, lost: set[str]) -> bool:
    """Is a lost element ahead of the column on the leg it is driving?"""
    leg = c.legs[i]
    if now >= leg.t_out:
        return False
    k = H.corridors[leg.arc[0]]
    frac = (now - leg.t_in) / max(leg.t_out - leg.t_in, 1e-9)
    for e in k.arc_elements(leg.arc[1]):
        if e in lost and k.element_chainage(e, leg.arc[1]) / max(k.length_m, 1.0) > frac:
            return True
    return False


def _remaining_arcs(c: Credit, pos: Position) -> tuple:
    if pos.state == "waiting":
        return tuple(l.arc for l in c.legs)
    if pos.state == "moving":
        return tuple(l.arc for l in c.legs[pos.leg:])
    return ()


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def handle_incident(state: PlanState, inc: Incident) -> IncidentOutcome:
    t0 = time.perf_counter()
    H, G = state.H, state.H.G
    now = inc.time_min
    edges, elements, center = locate(H, inc)
    pos = positions(H, state.sched, now)
    notes: list[str] = []
    grading = f"{inc.reliability}{inc.credibility}"
    before = {e for e in H.elements if _closed(G, H, e)}
    changed: list = []
    delays: dict[str, float] = {}
    pulled: set[str] = set()

    # 1-2. beliefs and map
    q = report_probability(inc.reliability, inc.credibility)
    if inc.type in CLOSURE_TYPES:
        state.belief.add(Report(inc.id, "closure", edges, q, now, inc.credibility == 1, duration=inc.duration_min, grading=grading))
    elif inc.type == IncidentType.THREAT_ZONE:
        state.belief.add(Report(inc.id, "threat", edges, q, now, inc.credibility == 1, severity=inc.severity, duration=inc.duration_min,
                                center=center, radius=inc.radius_m, grading=grading))
    elif inc.type == IncidentType.WEATHER:
        state.belief.add(Report(inc.id, "weather", edges, q, now, inc.credibility == 1, severity=inc.severity, duration=inc.duration_min,
                                grading=grading))
    elif inc.type == IncidentType.CLEARED:
        state.belief.clear(edges, now)
    elif inc.type == IncidentType.RECCE_RESULT:
        for u, v in edges:
            set_edge_attr(G, u, v, **inc.attrs)
            changed.append((u, v))
        for e in elements:
            H.elements[e].attrs.update(inc.attrs)
    changed = sorted(set(changed) | set(state.belief.apply(now)), key=str)
    for ctx in {id(state.ctx): state.ctx, id(state.plan_ctx): state.plan_ctx}.values():
        for g in ctx.groups.values():
            g.coster.recompute(changed)
    after = {e for e in H.elements if _closed(G, H, e)}
    newly = sorted(after - before)
    lost = set(newly)

    # which units are touched
    affected: list[str] = []
    turn_back: dict[str, float] = {}
    for uid, c in state.sched.credits.items():
        p = pos.get(uid)
        if p is None or p.state == "arrived":
            continue
        rem = _remaining_arcs(c, p)
        if lost and any(set(H.corridors[a[0]].elements) & lost for a in rem):
            affected.append(uid)
            if p.state == "moving" and _ahead_on_leg(H, c, p.leg, now, lost):
                turn_back[uid] = TURN_AROUND_MIN
        elif inc.type == IncidentType.THREAT_ZONE and set(changed) & {e for a in rem for e in H.arc_edges(a)}:
            affected.append(uid)
    if inc.type == IncidentType.BREAKDOWN:
        units = [u for u in state.ctx.units.values() if u.convoy_id == inc.convoy_id or u.id == inc.convoy_id]
        units = [u for u in units if pos.get(u.id) and pos[u.id].state != "arrived"]
        if units:
            u = units[0]
            delays[u.id] = inc.duration_min or DEFAULT_BREAKDOWN_MIN
            p = pos[u.id]
            c = state.sched.credits[u.id]
            if p.state == "moving" and can_pull_aside(H, c.legs[p.leg].arc, state.ctx.groups[u.group_id].profile.width_m):
                pulled.add(u.id)
            affected.append(u.id)
            notes.append(f"{u.id} stops {delays[u.id]:g} min" + (" (pulls aside: the road stays open)" if u.id in pulled else
                                                                (" (blocks the road)" if p.state == "moving" else " before departure")))
    designated_hit = sorted({nr.name for nr in state.ref.routes.values() if lost & nr.route.elements})

    # 3-4. mode and new designations
    playbook = None
    if inc.type == IncidentType.BREAKDOWN:
        mode = "reschedule"
    elif inc.type == IncidentType.RECCE_RESULT:
        mode = "recce"
    elif not affected and not designated_hit:
        mode = "logged"
    elif len(newly) == 1 and inc.type in CLOSURE_TYPES and any(pb.element == newly[0] for pb in state.playbooks):
        mode = "playbook"
        playbook = next(pb for pb in state.playbooks if pb.element == newly[0])
    else:
        mode = "repair"
    summary = _summary(H, inc, edges, newly, changed)
    out = IncidentOutcome(inc, mode, summary, changed, newly, sorted(affected), playbook.id if playbook else None, notes=notes)
    if mode == "logged":
        out.seconds = time.perf_counter() - t0
        state.log.append(f"{hhmm(now)} {inc.id}: {summary}; logged")
        return out

    old_sched, old_ref = state.sched, state.ref
    new_routes: dict[str, Route] = {}
    if mode in ("playbook", "repair", "recce"):
        restart = {uid: (p.node if uid not in turn_back else state.sched.credits[uid].legs[p.leg].start,
                         p.time if uid not in turn_back else now + (now - state.sched.credits[uid].legs[p.leg].t_in) + TURN_AROUND_MIN)
                   for uid, p in pos.items() if p.state != "arrived"}
        fixed_routes: dict[str, Route] = {}
        if playbook is not None:
            for uid in affected:
                pctx = state.plan_ctx if uid in state.plan_ctx.units else state.ctx  # playbooks are built in the plan's context
                gid = pctx.units[uid].group_id
                grp = pctx.groups[gid]
                resp = playbook.responses.get(gid)
                arcs = _walk(H, resp, restart[uid][0], grp.destination) if resp else None
                if arcs is not None:
                    fixed_routes[uid] = make_route(H, grp, arcs, set(), allowed_halt_nodes(H, grp))
        free = set(affected) if mode == "repair" else (set(pos) if mode == "recce" else set())
        design, crit, ctx_r = _replan(state, now, restart, free, fixed_routes, pos, notes)
        state.design, state.crit, state.plan_ctx = design, crit, ctx_r
        for uid, p in pos.items():
            if p.state == "arrived":
                continue
            gid = ctx_r.units[uid].group_id if uid in ctx_r.units else None
            r = design.msr.get(gid) if gid else None
            if r is None:
                continue
            old = _remaining_arcs(state.sched.credits[uid], p)
            old_rest = old[1:] if p.state == "moving" else old
            if uid in turn_back or tuple(r.arcs) != tuple(old_rest):
                new_routes[uid] = r
        state.ref = refine(H, ctx_r.groups, design, crit, previous=old_ref)
        out.design, out.ref = design, state.ref
        out.restoration, out.recce = _restore(state, design, crit, ctx_r, now)

    # 5. movement credits
    tasks, fixed, kept = replan_tasks(H, state.ctx, state.sched, now, new_routes, delays, pulled, turn_back)
    state.sched = schedule(H, state.ctx, tasks=tasks, fixed=fixed, kept=kept, repair=True, use_cpsat=state.use_cpsat,
                           time_limit_s=state.time_limit_s)
    out.sched = state.sched
    # 6. verification of the remaining plan
    vctx = MissionContext(state.ctx.mission, {**state.ctx.groups, **state.plan_ctx.groups}, state.ctx.units, state.ctx.profiles,
                          state.ctx.convoy_group)
    out.verification = verify(H, vctx, state.design, state.ref, state.sched, state.crit)
    # 8. FRAGO
    state.frago_no += 1
    out.diff = _diff(H, state, inc, old_sched, old_ref, pos, new_routes, turn_back, out)
    out.frago, out.frago_source = write_frago(out.diff, state.llm)
    out.seconds = time.perf_counter() - t0
    state.log.append(f"{hhmm(now)} {inc.id}: {summary}; {mode}" + (f" {playbook.id}" if playbook else ""))
    return out


def _closed(G, H: CorridorGraph, eid: str) -> bool:
    el = H.elements[eid]
    if el.kind == "articulation":
        return False
    return any(G[u][v].get("closed") for k in el.edges for u, v in (k, k[::-1]) if G.has_edge(u, v))


def _walk(H: CorridorGraph, resp, node: Any, dst: Any) -> tuple | None:
    arcs: list = []
    seen = set()
    while node != dst:
        if node in seen:
            return None
        seen.add(node)
        a = resp.next_arc(node)
        if a is None:
            return None
        arcs.append(a)
        node = H.arc_end(a)
    return tuple(arcs)


def _summary(H: CorridorGraph, inc: Incident, edges, newly, changed) -> str:
    what = inc.type.value.replace("_", " ")
    if newly:
        return f"{what}: {', '.join(H.elements[e].name for e in newly)} closed ({len(edges)} road segments)"
    if inc.type == IncidentType.THREAT_ZONE:
        return f"{what}: threat raised on {len(changed)} road segments"
    if inc.type == IncidentType.RECCE_RESULT:
        return f"{what}: {', '.join(f'{k}={v}' for k, v in inc.attrs.items())} on {len(edges)} road segments"
    if inc.type == IncidentType.BREAKDOWN:
        return f"{what}: {inc.convoy_id} stopped {inc.duration_min or DEFAULT_BREAKDOWN_MIN:g} min"
    return f"{what}: {len(changed)} road segments changed"


def _replan(state: PlanState, now: float, restart: dict[str, tuple[Any, float]], free_units: set[str],
            fixed_routes: dict[str, Route], pos: dict[str, Position], notes: list[str]) -> tuple[Design, Criticality, MissionContext]:
    """S5 on the remaining mission from the current positions (playbook routes or unaffected main routes fixed)."""
    H = state.H
    m = state.ctx.mission
    lead: dict[str, str] = {}
    for uid, u in state.ctx.units.items():
        if uid in restart and u.convoy_id not in lead:
            lead[u.convoy_id] = uid
    convoys = []
    for c in m.convoys:
        uid = lead.get(c.id)
        if uid is None:
            continue  # arrived
        node, t = restart[uid]
        convoys.append(c.model_copy(update={"origin": LocationRef(node=node), "earliest_departure_min": max(t, c.earliest_departure_min)}))
    mission_r = m.model_copy(update={"convoys": convoys})
    base = dict(next(iter(state.ctx.groups.values())).coster.overrides)
    ctx_r = build_context(H, mission_r, state.requirements, overrides=base)
    prev_msr: dict[str, Route] = {}
    prev_asr: dict[str, list[Route]] = {}
    seeds: dict[str, list[tuple]] = {}
    fixed: dict[str, Route] = {}
    moving = set()
    for gid, g in ctx_r.groups.items():
        uid = lead[g.convoys[0].id]
        node = restart[uid][0]
        old_gid = state.plan_ctx.units[uid].group_id if uid in state.plan_ctx.units else gid
        if pos[uid].state == "moving":
            moving.add(gid)
        for role, routes in (("MSR", [state.design.msr.get(old_gid)]), ("ASR", state.design.asrs.get(old_gid, []))):
            for r in routes:
                if r is None:
                    continue
                dps = r.dps(H)
                if node in dps[:-1]:
                    suffix = tuple(r.arcs[dps.index(node):])
                    seeds.setdefault(gid, []).append(suffix)
                    rr = make_route(H, g, suffix, set(), allowed_halt_nodes(H, g))
                    if role == "MSR":
                        prev_msr[gid] = rr
                    else:
                        prev_asr.setdefault(gid, []).append(rr)
        if uid in fixed_routes:
            seeds.setdefault(gid, []).insert(0, tuple(fixed_routes[uid].arcs))
    cands = {gid: generate_candidates(H, g, seed_routes=seeds.get(gid)) for gid, g in ctx_r.groups.items()}
    for gid, g in ctx_r.groups.items():
        uid = lead[g.convoys[0].id]
        if uid in fixed_routes:
            fixed[gid] = next((r for r in cands[gid].routes if r.key == tuple(fixed_routes[uid].arcs)), fixed_routes[uid])
        elif uid not in free_units and gid in prev_msr and all(g.coster.arc_ok(a) for a in prev_msr[gid].arcs):
            fixed[gid] = next((r for r in cands[gid].routes if r.key == prev_msr[gid].key), prev_msr[gid])
    crit = assess(H, ctx_r.groups, cands, base_likelihood=state.crit.base_likelihood or None)
    previous = Design(state.design.name, state.order, state.eps, prev_msr, prev_asr, set(), {}, {}, {})
    order = ["stability"] + state.order if state.stability_first else state.order + ["stability"]
    prob = DesignProblem(H, ctx_r.groups, cands, crit, phase=m.phase, fixed_msr=fixed, previous=previous, moving=moving)
    design = solve(prob, order, state.eps, state.design.name, time_limit_s=state.time_limit_s, use_cpsat=state.use_cpsat,
                   description=state.design.description)
    for gid, why in design.unplanned.items():
        notes.append(f"{gid}: no route ({why})")
    return design, crit, ctx_r


def _restore(state: PlanState, design: Design, crit: Criticality, ctx_r: MissionContext, now: float) -> tuple[list[str], list[ReconTask]]:
    """Step 7: report single points of failure left after the repair, with the options."""
    H = state.H
    lines: list[str] = []
    spof: dict[str, list[str]] = {}
    for gid, msr in design.msr.items():
        for e in msr.ordered_elements(H):
            if H.elements[e].kind == "articulation":
                continue
            if all(e in a.elements for a in design.asrs.get(gid, [])):
                spof.setdefault(e, []).append(gid)
    tasks: list[ReconTask] = []
    if any(e in crit.irreducible.get(g, set()) for e, gs in spof.items() for g in gs):
        try:
            tasks = value_of_information(H, ctx_r, state.requirements, design, crit, state.order, state.eps, now=now,
                                         use_cpsat=state.use_cpsat, time_limit_s=state.time_limit_s, rebase=True)
        except Exception as exc:  # noqa: BLE001 - restoration options must not break the loop
            lines.append(f"reconnaissance values not computed: {exc}")
    critpoint = next((r for g in ctx_r.groups.values() for r in g.active.of("PRO.CRITPOINT")), None)
    for e, gs in sorted(spof.items()):
        el = H.elements[e]
        irr = [g for g in gs if e in crit.irreducible.get(g, set())]
        acc = [g for g in gs if g not in irr]
        if acc:
            lines.append(f"{el.name}: single point of failure accepted for {', '.join(acc)} (no alternate within the limits)")
        if not irr:
            continue
        opts = []
        rt = [t for t in tasks if any(g in t.removes_spof or g in t.affected for g in irr)]
        for t in rt[:2]:
            opts.append(f"reconnoitre {t.name} ({t.id}: {t.short(set(irr))})")
        prot = "protect it: traffic control post, escort"
        if critpoint is not None:
            prot += f", no halts within {critpoint.p('no_stop_within_m'):g} m ({critpoint.id})"
        opts.append(prot)
        if el.kind == "bridge":
            opts.append("request a gap-crossing asset")
        opts.append("accept the risk")
        lines.append(f"{el.name}: irreducible single point of failure for {', '.join(irr)}. Options: " + "; ".join(opts))
    return lines, tasks


def _diff(H: CorridorGraph, state: PlanState, inc: Incident, old: Schedule, old_ref: Refinement, pos: dict[str, Position],
          new_routes: dict[str, Route], turn_back: dict[str, float], out: IncidentOutcome) -> PlanDiff:
    units: list[UnitChange] = []
    ref = state.ref
    for uid, c in state.sched.credits.items():
        p = pos.get(uid)
        if p is None or p.state == "arrived":
            continue
        oc = old.credits.get(uid)
        g = state.ctx.groups[state.ctx.units[uid].group_id]
        if uid in new_routes:
            r = new_routes[uid]
            gid = state.plan_ctx.units[uid].group_id if uid in state.plan_ctx.units else g.id
            own = [ref.assignment[gid]["MSR"]] if ref is not None and gid in ref.assignment else []
            story = route_story(H, r, ref, own)
            note = f"turn back now to {H.label(oc.legs[p.leg].start)}" if uid in turn_back and oc is not None else ""
            units.append(UnitChange(uid, g.id, "reroute", route=story, depart=c.depart if p.state == "waiting" else None,
                                    arrive=c.arrive, old_arrive=oc.arrive if oc else None, deadline=c.deadline, note=note))
        elif oc is not None and (abs(c.depart - oc.depart) >= 0.5 or abs(c.arrive - oc.arrive) >= 0.5):
            action = "hold" if c.depart > oc.depart + 0.5 and p.state == "waiting" else "retime"
            nm = ref.name_of(c.route) if ref is not None else None
            units.append(UnitChange(uid, g.id, action, route=nm or "",
                                    depart=c.depart if p.state == "waiting" else None, arrive=c.arrive,
                                    old_arrive=oc.arrive, deadline=c.deadline,
                                    note="new times at control points in annex" if p.state == "moving" else ""))
    statuses = []
    for nm, nr in ref.routes.items():
        if nr.status == "prohibited":
            statuses.append(f"{nr.role} {nm} prohibited ({nr.status_reason})")
    for nm, nr in old_ref.routes.items():
        if nm in ref.routes:
            continue
        hit = sorted(set(out.newly_closed) & nr.route.elements)
        if hit:
            statuses.append(f"{nr.role} {nm} prohibited (closed: {', '.join(H.elements[e].name for e in hit)})")
        elif any(p.state != "arrived" for uid, p in pos.items() if state.ctx.units[uid].group_id in nr.groups):
            statuses.append(f"{nr.role} {nm} cancelled")
    for nm, nr in ref.routes.items():
        if nm not in old_ref.routes:
            statuses.append(f"new {nr.role} {nm} for {', '.join(nr.groups)}: {route_story(H, nr.route, None)} ({nr.status})")
        else:
            o = old_ref.routes[nm]
            if (o.role, o.status) != (nr.role, nr.status) and nr.status != "prohibited":
                statuses.append(f"{nm} now {nr.role} of {', '.join(nr.groups)} ({nr.status})")
    control = []
    old_posts = {(p.node, p.element) for p in old_ref.posts}
    for p in ref.posts:
        if (p.node, p.element) not in old_posts and p.element:
            el = H.elements[p.element]
            control.append(f"{p.id} at {H.label(p.node)}" + (f" ({el.name})" if el.kind != "articulation" else "")
                           + (" with escort" if p.escort else ""))
    text = inc.text.strip().rstrip(".") or out.summary
    situation = text + "." if "graded" in text.lower() else f"{text} (report graded {inc.reliability}{inc.credibility})."
    if out.playbook:
        situation += f" Playbook {out.playbook} applies."
    signal = []
    if units:
        signal.append("report passage of release points to movement control")
    return PlanDiff(state.frago_no, state.ctx.mission.id, situation, units, statuses, control,
                    signal=signal, annex="new movement credits in annex" if units else "")


__all__ = ["IncidentOutcome", "PlanState", "Position", "handle_incident", "locate", "positions"]

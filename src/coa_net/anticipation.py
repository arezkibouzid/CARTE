"""S8 (anticipation): contingency playbooks and reconnaissance tasks (methodology 5, S8; Appendix B.4, C.4, C.5).

Playbooks. For every loss scenario with p >= p_min (or among the highest
impacts) that touches the designated network, the response is computed and
briefed before departure. For each affected group:

* the designated alternate that survives the loss, or else the best route
  that avoids the lost element (an unprepared fallback), or nothing (stranded);
* a shortest-path tree towards the destination on H minus the element, which
  gives the next corridor from every decision point, so the response holds
  wherever the column is when it learns of the loss (on the designated
  alternate, the alternate is followed);
* the decision points where the response leaves the main route;
* movement credits re-solved for the scenario, control measures and a FRAGO draft.

Reconnaissance tasks. For each critical element with an unknown attribute
that blocks at least one group (forbid or recce policy), the value of
information

    VOI(u) = sum_o q_o (J(x0* | o) - J(xo* | o))

compares the plan made without knowing u (x0*) with the plan re-made after each
answer o. Answers are classes bounded by the groups' needs (for a bridge, the
groups' load classes); priors q_o come from the known elements of the same kind
and road class, with Laplace smoothing. J is the lexicographic objective
vector, so the value is reported on the highest-priority objective that
changes, with the probability that the answer removes an irreducible single
point of failure. A task is issued only if the answer can arrive before the
affected groups leave.
"""

from __future__ import annotations

import heapq
import itertools
from dataclasses import dataclass, field, replace
from typing import Any

from .candidates import Route, _banned_sets, allowed_halt_nodes, generate_candidates, make_route
from .corridors import Arc, CorridorGraph
from .criticality import Criticality, assess
from .design import ETA, Design, DesignProblem, _recourse, evaluate, solve
from .graph import node_xy, point_distance, ukey
from .mission import Group, MissionContext, build_context
from .orders import PlanDiff, UnitChange, frago_template
from .refine import Refinement, road_name, route_story
from .requirements import Requirement
from .schedule import Schedule, hhmm, schedule

INF = float("inf")
RECCE_SPEED_KMH = 40.0  # reconnaissance patrol, road speed (assumption)
RECCE_DETOUR = 1.3  # road distance / straight-line distance
RECCE_TASK_MIN = 15.0  # time on site to classify a bridge (assumption)


# ---------------------------------------------------------------------------
# Playbooks
# ---------------------------------------------------------------------------


@dataclass
class GroupResponse:
    group: str
    status: str  # unaffected | alternate-lost | designated | fallback | stranded
    route: Route | None  # route from the origin in the scenario
    via: str | None  # name of the designated route taken (None for an unprepared fallback)
    decisions: list[tuple[Any, Arc | None]]  # decision points of the MSR where the response leaves it
    tree: dict[Any, tuple[Arc, Any]] = field(default_factory=dict)
    text: str = ""
    arrival: dict[str, float] = field(default_factory=dict)  # unit -> arrival in the scenario's credits
    _on_route: dict[Any, Arc] = field(default_factory=dict, repr=False)

    def next_arc(self, node: Any) -> Arc | None:
        """Corridor to take from a decision point once the loss is known."""
        if node in self._on_route:
            return self._on_route[node]
        t = self.tree.get(node)
        return t[0] if t else None


@dataclass
class Playbook:
    id: str
    scenario: str
    element: str
    name: str
    p: float
    impact: float
    trigger: str
    responses: dict[str, GroupResponse]
    control: list[str]
    schedule: Schedule | None
    frago: str = ""

    @property
    def affected(self) -> list[str]:
        return [g for g, r in self.responses.items() if r.status in ("designated", "fallback", "stranded")]

    def entry(self) -> str:
        """Playbook entry in the format of Appendix C.4."""
        lines = [f"{self.id} | Trigger: {self.trigger} | Affected: {', '.join(self.affected) or 'none'}"]
        for g, r in self.responses.items():
            if r.status != "unaffected":
                lines.append(f"{g}: {r.text}")
        if self.control:
            lines.append("Control: " + "; ".join(self.control) + f". FRAGO draft: {self.id}-F.")
        return "\n".join(lines)


def _incoming(H: CorridorGraph) -> dict[Any, list[tuple[Arc, Any]]]:
    inc: dict[Any, list[tuple[Arc, Any]]] = {}
    for u, lst in H.out.items():
        for arc, v in lst:
            inc.setdefault(v, []).append((arc, u))
    return inc


def next_arc_tree(H: CorridorGraph, group: Group, banned: set[str] | frozenset = frozenset(),
                  banned_corridors: set[str] | frozenset = frozenset()) -> dict[Any, tuple[Arc, Any]]:
    """Shortest-path tree (time) towards the group's destination, without the banned elements:
    for every decision point that can still reach it, the next arc and the node it leads to."""
    bc, bn = _banned_sets(H, banned)
    bc = bc | set(banned_corridors)
    dst = group.destination
    arcs = group.coster.arcs
    inc = _incoming(H)
    dist = {dst: 0.0}
    nxt: dict[Any, tuple[Arc, Any]] = {}
    tick = itertools.count()
    heap = [(0.0, next(tick), dst)]
    while heap:
        d, _, v = heapq.heappop(heap)
        if d > dist.get(v, INF) or (v in bn and v != dst):
            continue
        for arc, u in inc.get(v, ()):
            ae = arcs.get(arc)
            if arc[0] in bc or ae is None or not ae.ok:
                continue
            nd = d + ae.time_min
            if nd < dist.get(u, INF):
                dist[u] = nd
                nxt[u] = (arc, v)
                heapq.heappush(heap, (nd, next(tick), u))
    return nxt


def tree_path(tree: dict[Any, tuple[Arc, Any]], src: Any, dst: Any) -> tuple[Arc, ...] | None:
    out: list[Arc] = []
    n = src
    seen = set()
    while n != dst:
        if n in seen or n not in tree:
            return None
        seen.add(n)
        arc, n = tree[n]
        out.append(arc)
    return tuple(out)


def _loss_index(H: CorridorGraph, route: Route, lost: frozenset) -> int | None:
    """Index of the first arc of the route on which (or at whose end) a lost element lies."""
    for i, a in enumerate(route.arcs):
        if set(H.corridors[a[0]].elements) & lost:
            return i
        if f"AP-{H.arc_end(a)}" in lost and i < len(route.arcs) - 1:
            return i
    return None


def _turn(H: CorridorGraph, node: Any, arc: Arc | None) -> str:
    if arc is None:
        return f"at {H.label(node)} no route left: hold and report"
    return f"at {H.label(node)} take {road_name(H, arc)} towards {H.label(H.arc_end(arc))}"


def group_response(H: CorridorGraph, ref: Refinement | None, design: Design, crit: Criticality, group: Group,
                   sid: str, lost: frozenset) -> GroupResponse:
    gid = group.id
    msr = design.msr[gid]
    name_of = (lambda r: ref.name_of(r)) if ref is not None else (lambda r: None)
    if not (lost & msr.elements):
        gone = [name_of(a) or "alternate" for a in design.asrs.get(gid, []) if lost & a.elements]
        if gone:
            return GroupResponse(gid, "alternate-lost", msr, name_of(msr), [],
                                 text=f"main route unaffected; {', '.join(gone)} unusable: no designated alternate left for its elements")
        return GroupResponse(gid, "unaffected", msr, name_of(msr), [], text="unaffected")
    tree = next_arc_tree(H, group, lost)
    r = design.recourse.get((gid, sid))
    if r is not None and not (lost & r.elements):
        status, route, via = "designated", r, name_of(r)
    else:
        fb = crit.fallback.get((gid, sid))
        route = fb[2] if fb is not None and fb[2] is not None and not (lost & fb[2].elements) else None
        if route is None:
            arcs = tree_path(tree, group.origin, group.destination)
            route = make_route(H, group, arcs, set(), allowed_halt_nodes(H, group)) if arcs else None
        status, via = ("fallback", None) if route is not None else ("stranded", None)
    resp = GroupResponse(gid, status, route, via, [], tree)
    if route is not None:
        resp._on_route = {H.arc_start(a): a for a in route.arcs}
    k = _loss_index(H, msr, lost)
    for i, a in enumerate(msr.arcs[: (k if k is not None else len(msr.arcs) - 1) + 1]):
        n = H.arc_start(a)
        nxt = resp.next_arc(n)
        if nxt != a:
            resp.decisions.append((n, nxt))
    msr_name = name_of(msr) or "main route"
    lost_names = ", ".join(H.elements[e].name for e in sorted(lost))
    if status == "designated":
        first = resp.decisions[0][0] if resp.decisions else group.origin
        text = f"switch from {msr_name} to ASR {via or 'alternate'} at {H.label(first)} ({route.length_m / 1000:.1f} km, {route.time_min:.1f} min)"
    elif status == "fallback":
        irr = "irreducible single point of failure" if any(e in crit.irreducible.get(gid, set()) for e in lost) else "accepted single point of failure"
        text = (f"no designated alternate ({irr}). Fallback, not prepared: {route_story(H, route, ref, [msr_name])} "
                f"({route.length_m / 1000:.1f} km, {route.time_min:.1f} min)")
    else:
        text = f"no route avoids {lost_names}: hold, report, request a gap-crossing asset"
    later = [d for d in resp.decisions[1:]][:3]
    if later and status != "stranded":
        text += ". If already past " + H.label(resp.decisions[0][0]) + ": " + "; ".join(_turn(H, n, a) for n, a in later)
    resp.text = text
    return resp


def build_playbooks(H: CorridorGraph, ctx: MissionContext, design: Design, crit: Criticality, ref: Refinement | None = None,
                    p_min: float = 0.05, limit: int | None = None, use_cpsat: bool = True, time_limit_s: float = 5.0,
                    mission_id: str | None = None) -> list[Playbook]:
    """One playbook per likely or high-impact loss scenario that touches the designated network."""
    groups = {g: ctx.groups[g] for g in design.msr}
    top = set(sorted(crit.impact, key=lambda e: -crit.impact[e])[: max(1, len(crit.impact) // 10)])
    used = {e for g in design.msr for r in design.routes(g) for e in r.elements}
    chosen = [s for s in crit.scenarios[1:] if (s.p >= p_min or set(s.lost) & top) and set(s.lost) & used]
    chosen.sort(key=lambda s: -(s.p * sum(crit.impact.get(e, 0.0) for e in s.lost)))
    if limit is not None:
        chosen = chosen[:limit]
    closed_p = min((g.active.param("SEG.STATUS", "closed_p", 0.5) for g in groups.values()), default=0.5)
    critpoint = [r for g in groups.values() for r in g.active.of("PRO.CRITPOINT")]
    books = []
    for i, s in enumerate(chosen, start=1):
        e = next(iter(sorted(s.lost)))
        el = H.elements[e]
        responses = {gid: group_response(H, ref, design, crit, g, s.id, s.lost) for gid, g in groups.items()}
        routes = {gid: (r.route if r.route is not None else design.msr[gid]) for gid, r in responses.items() if r.status != "stranded"}
        sched = schedule(H, ctx, routes, use_cpsat=use_cpsat, time_limit_s=time_limit_s) if routes else None
        if sched is not None:
            for uid, c in sched.credits.items():
                if c.group in responses:
                    responses[c.group].arrival[uid] = c.arrive
        for gid, r in responses.items():
            if r.arrival and r.status in ("designated", "fallback"):
                g = groups[gid]
                arr = max(r.arrival.values())
                r.text += f". Arrival {hhmm(arr)}" + (f" (deadline {hhmm(g.deadline)})" if g.deadline is not None else "")
        control = _control_measures(H, ref, design, responses, s.lost, critpoint)
        pb = Playbook(f"PB-{i:02d}", s.id, e, el.name, s.p, sum(crit.impact.get(x, 0.0) for x in s.lost),
                      f"{el.name} closed (closure probability >= {closed_p:g})", responses, control, sched)
        pb.frago = frago_template(playbook_diff(H, ctx, pb, ref, mission_id or ctx.mission.id))
        books.append(pb)
    return books


def _control_measures(H: CorridorGraph, ref: Refinement | None, design: Design, responses: dict[str, GroupResponse],
                      lost: frozenset, critpoint) -> list[str]:
    out = []
    if ref is not None:
        for nm, nr in ref.routes.items():
            if lost & nr.route.elements:
                out.append(f"{nr.role} {nm} prohibited")
    new_elements: list[str] = []
    for gid, r in responses.items():
        if r.status in ("designated", "fallback") and r.route is not None:
            for e in r.route.ordered_elements(H):
                if e not in design.msr[gid].elements and e not in new_elements and H.elements[e].kind != "articulation":
                    new_elements.append(e)
    posts = {p.element: p for p in ref.posts} if ref is not None else {}
    for e in new_elements:
        el = H.elements[e]
        p = posts.get(e)
        out.append(f"reinforce {p.id} at {el.name}" if p else f"post a traffic control post at {el.name}")
        if critpoint and el.kind in set(critpoint[0].p("element_kinds") or []):
            out.append(f"no halts within {critpoint[0].p('no_stop_within_m'):g} m of {el.name} ({critpoint[0].id})")
    return out


def playbook_diff(H: CorridorGraph, ctx: MissionContext, pb: Playbook, ref: Refinement | None, mission_id: str) -> PlanDiff:
    units = []
    for gid in pb.affected:
        r = pb.responses[gid]
        g = ctx.groups[gid]
        for uid, u in ctx.units.items():
            if u.group_id != gid:
                continue
            if r.status == "stranded":
                units.append(UnitChange(uid, gid, "stranded", rp=H.label(g.destination)))
                continue
            story = f"ASR {r.via}" if r.status == "designated" and r.via else route_story(H, r.route, ref)
            c = pb.schedule.credits.get(uid) if pb.schedule else None
            units.append(UnitChange(uid, gid, "reroute", route=story, depart=c.depart if c else None,
                                    arrive=c.arrive if c else None, deadline=g.deadline))
    situation = f"Playbook {pb.id} triggered: {pb.trigger}."
    return PlanDiff(1, mission_id, situation, units, control=list(pb.control), annex=f"movement credits in annex {pb.id}-MC",
                    signal=[f"report the trigger to movement control; units holding {pb.id} apply it on the trigger message"])


# ---------------------------------------------------------------------------
# Reconnaissance tasks and value of information
# ---------------------------------------------------------------------------

ATTRIBUTE_OF = {"bridge load class unknown": "mlc", "tunnel clearance unknown": "max_height_m", "width unknown": "width_m"}
ATTRIBUTE_TEXT = {"mlc": "MLC", "max_height_m": "clearance", "width_m": "width"}


@dataclass
class Answer:
    label: str
    prior: float
    value: float
    objectives: dict[str, float]  # J(xo* | o)
    baseline: dict[str, float]  # J(x0* | o)
    irreducible: dict[str, set[str]]
    changes: list[tuple[str, str, str]]  # (group, kind: msr | alternate | spof, text)

    def gains(self, groups: set[str] | None = None) -> str:
        """What the answer brings: alternates and single points of failure removed (main-route switches only with them)."""
        by: dict[str, list[tuple[str, str]]] = {}
        for g, kind, text in self.changes:
            if groups is None or g in groups:
                by.setdefault(g, []).append((kind, text))
        parts = []
        for g, items in by.items():
            kinds = {k for k, _ in items}
            keep = [t for k, t in items if k in ("alternate", "spof") or (k == "msr" and "spof" in kinds)]
            if keep:
                parts.append(f"{g} " + " and ".join(keep))
        return "; ".join(parts)


@dataclass
class ReconTask:
    id: str
    element: str
    name: str
    attribute: str
    question: str
    answers: list[Answer]
    level: str | None  # highest-priority objective on which the answer has value
    value: float
    values: dict[str, float]
    removes_spof: dict[str, float]  # group -> probability that the answer removes an irreducible SPOF
    affected: list[str]
    eta: float
    deadline: float | None
    feasible: bool
    why: str = ""
    priority: int = 0

    def short(self, groups: set[str] | None = None) -> str:
        parts = [f"if {a.label}: {a.gains(groups)}" for a in self.answers if a.gains(groups)]
        return "; ".join(parts) or "no answer changes the plan"

    def entry(self) -> str:
        """Task in the format of Appendix C.5."""
        when = f"Report before {', '.join(self.affected)} depart{'s' if len(self.affected) == 1 else ''} ({hhmm(self.deadline)})" \
            if self.deadline is not None else ("No deadline (affected groups already moving: for the next movement)" if self.affected
                                               else "No deadline (no group's plan depends on the answer)")
        ok = "" if self.feasible or self.deadline is None else f" | NOT IN TIME (answer expected {hhmm(self.eta)})"
        return f"{self.id} | {self.question} | {when}{ok}\nWhy: {self.why}\nPriority {self.priority}."


def recce_candidates(H: CorridorGraph, groups: dict[str, Group]) -> dict[str, str]:
    """Critical elements whose unknown attribute blocks at least one group: element -> attribute."""
    out: dict[str, str] = {}
    for eid, el in H.elements.items():
        if el.kind == "articulation":
            continue
        for g in groups.values():
            for u, v in el.edges:
                for e in ((u, v), (v, u)):
                    ev = g.coster.evals.get(e)
                    for _rid, detail in (ev.recce if ev else []):
                        attr = ATTRIBUTE_OF.get(detail)
                        if attr and eid not in out:
                            out[eid] = attr
    return out


def _needs(attr: str, groups: dict[str, Group]) -> list[float]:
    vals = set()
    for g in groups.values():
        p = g.profile
        if attr == "mlc":
            for r in g.active.of("SEG.LOAD", ("T0", "T1")):
                if r.p("check") in ("bridge_mlc", "both"):
                    vals.add(float(p.mlc + (r.p("margin_mlc") or 0)))
        elif attr == "max_height_m":
            for r in g.active.of("SEG.GAUGE", ("T0", "T1")):
                if r.p("check") in ("height", "both"):
                    vals.add(round(p.height_m + (r.p("height_margin_m") or 0.0), 2))
        elif attr == "width_m":
            for r in g.active.of("SEG.GAUGE", ("T0", "T1")):
                if r.p("check") in ("width", "both"):
                    vals.add(round(p.width_m + (r.p("width_margin_m") or 0.0), 2))
    return sorted(vals)


def answer_classes(attr: str, needs: list[float]) -> list[tuple[str, float]]:
    """Answer classes bounded by the groups' needs, each with a representative value (its lower bound)."""
    t = ATTRIBUTE_TEXT.get(attr, attr)
    unit = "" if attr == "mlc" else " m"
    fmt = (lambda x: f"{x:g}")
    out = [(f"{t} < {fmt(needs[0])}{unit}", needs[0] - (1 if attr == "mlc" else 0.1))]
    for a, b in zip(needs, needs[1:]):
        hi = fmt(b - 1) if attr == "mlc" else f"< {fmt(b)}"
        out.append((f"{t} {fmt(a)}-{hi}{unit}" if attr == "mlc" else f"{t} {fmt(a)} to {hi}{unit}", a))
    out.append((f"{t} >= {fmt(needs[-1])}{unit}", needs[-1]))
    return out


def answer_priors(H: CorridorGraph, eid: str, attr: str, classes: list[tuple[str, float]]) -> list[float]:
    """Laplace-smoothed share of known elements of the same kind and road class in each answer class."""
    el = H.elements[eid]
    G = H.G

    def rank_of(e):
        u, v = next(iter(H.elements[e].edges))
        d = G[u][v] if G.has_edge(u, v) else G[v][u]
        return d.get("road_rank")

    def value_of(e):
        return H.elements[e].attrs.get(attr)

    peers = [e for e, x in H.elements.items() if x.kind == el.kind and e != eid and value_of(e) is not None]
    same = [e for e in peers if rank_of(e) == rank_of(eid)]
    pool = same or peers
    bounds = [v for _, v in classes[1:]]
    counts = [0] * len(classes)
    for e in pool:
        val = value_of(e)
        k = sum(1 for b in bounds if val >= b)
        counts[k] += 1
    n = len(pool)
    return [(c + 1) / (n + len(classes)) for c in counts]


def _base_overrides(ctx: MissionContext) -> dict:
    g = next(iter(ctx.groups.values()), None)
    return dict(g.coster.overrides) if g is not None else {}


def _alike(x, y) -> bool:
    """Same evaluation, or unusable in both worlds (route searches never enter an unusable arc)."""
    return x == y or (x is not None and y is not None and not x.ok and not y.ok)


def _routes_alike(H: CorridorGraph, g: Group, b: Group, ov: dict) -> bool:
    """True if group ``g`` (what-if world with overrides ``ov``) gets exactly the candidate routes of ``b`` (base world)."""
    if (g.origin, g.destination, tuple(g.via)) != (b.origin, b.destination, tuple(b.via)):
        return False
    cg, cb = g.coster, b.coster
    changed = {k for k in set(ov) | set(cb.overrides) if ov.get(k) != cb.overrides.get(k)}
    cids = set()
    for k in changed:
        k = tuple(k)
        for e in (k, k[::-1]):
            if not _alike(cg.evals.get(e), cb.evals.get(e)):
                return False
        cid = H.edge_corridor.get(ukey(*k))
        if cid is not None:
            cids.add(cid)
    return all(_alike(cg.arcs.get((c, d)), cb.arcs.get((c, d))) for c in cids for d in (1, -1))


def _overrides_key(ov: dict) -> tuple:
    """Hashable, order-independent key of an override set (road key -> {attribute: value})."""
    return tuple(sorted((repr(k), tuple(sorted((a, repr(v)) for a, v in d.items()))) for k, d in ov.items()))


def _lex_first(J0: dict[str, float], J1: dict[str, float], order: list[str]) -> tuple[str, float] | None:
    for k in order:
        d = J0.get(k, 0.0) - J1.get(k, 0.0)
        if abs(d) > ETA.get(k, 0.1):
            return k, d
    return None


def _design_changes(H: CorridorGraph, x0: Design, xo: Design, crit0: Criticality, crit_o: Criticality) -> list[tuple[str, str, str]]:
    out = []
    for gid, msr in xo.msr.items():
        old = x0.msr.get(gid)
        if old is not None and old.key != msr.key:
            out.append((gid, "msr", f"switches to {msr.describe(H)} (instead of {old.describe(H)})"))
        old_a = {a.key for a in x0.asrs.get(gid, [])}
        for a in xo.asrs.get(gid, []):
            if a.key not in old_a:
                covers = [H.elements[e].name for e in sorted(msr.elements - a.elements) if H.elements[e].kind != "articulation"]
                out.append((gid, "alternate", f"gains an alternate of {a.describe(H)}" + (f" avoiding {', '.join(covers)}" if covers else "")))
        gone = sorted(crit0.irreducible.get(gid, set()) - crit_o.irreducible.get(gid, set()))
        gone = [H.elements[e].name for e in gone if H.elements[e].kind != "articulation"]
        if gone:
            out.append((gid, "spof", f"loses its irreducible single point of failure on {', '.join(gone)}"))
    return out


def value_of_information(H: CorridorGraph, ctx: MissionContext, requirements: list[Requirement], design: Design,
                         crit: Criticality, order: list[str] | None = None, eps: dict[str, float] | None = None,
                         now: float | None = None, asset: Any = None, ref: Refinement | None = None,
                         elements: list[str] | None = None, use_cpsat: bool = True, time_limit_s: float = 5.0,
                         rebase: bool = False, max_elements: int | None = None,
                         world_cache: dict | None = None, base_cands: dict | None = None) -> list[ReconTask]:
    """Reconnaissance tasks ranked by value of information (highest-priority objective first).

    ``rebase``: x0* is re-planned without the information by the same procedure as the answers, so that what is
    counted as value comes from the answer and not from the procedure: after a repair, its constraints (stability,
    fixed routes); in the plan, the solver's time budget (on Scenario 1 the 5 s what-if solves of COA R found a design
    as good on the higher priorities and 13.4 veh.min faster than the COA's own 10 s solve, whatever the answer). An
    answer that leaves every group's roads as they are is not re-solved: its plan is x0*.

    ``world_cache``: what-if worlds (context, candidates, criticality) by override set, shared by the COAs of
    one plan (same mission, base context and criticality); only the design step depends on the COA.

    ``base_cands``: the candidate sets of ``ctx`` (the plan's S3). A group whose routing the answer does not
    change (same terminals; on every changed road the same evaluation, or unusable in both worlds) keeps
    its base candidates instead of regenerating identical ones."""
    order = list(order or design.order)
    eps = dict(eps if eps is not None else design.eps)
    full = ["lateness"] + [k for k in order if k != "lateness"]
    phase = ctx.mission.phase
    groups = ctx.groups
    cands_e = recce_candidates(H, groups)
    if elements is not None:
        cands_e = {e: a for e, a in cands_e.items() if e in elements}
    skipped: list[str] = []
    all_cands = dict(cands_e)
    if max_elements is not None and len(cands_e) > max_elements:
        # large maps: evaluate the elements closest to the designated network first (each costs two what-if plans)
        pts = [node_xy(H.G, n) for r in list(design.msr.values()) + [a for v in design.asrs.values() for a in v] for n in r.nodes]
        if pts:
            dist = {e: min(point_distance(H.G, H.elements[e].xy, p) for p in pts[:: max(1, len(pts) // 400)]) for e in cands_e}
            ranked = sorted(cands_e, key=lambda e: (dist[e], e))
        else:
            ranked = sorted(cands_e)
        skipped = ranked[max_elements:]
        cands_e = {e: cands_e[e] for e in ranked[:max_elements]}
    start = min((g.earliest for g in groups.values()), default=0.0)
    now = start - 60.0 if now is None else now
    if asset is None:
        first = min(groups.values(), key=lambda g: (-g.weight, g.earliest, g.id), default=None)
        asset = first.origin if first else None
    base = _base_overrides(ctx)

    def world_of(ov):
        key = _overrides_key(ov)
        world = world_cache.get(key) if world_cache is not None else None
        if world is None:
            ctx_o = build_context(H, ctx.mission, requirements, overrides=ov, base=ctx)
            cands_o = {}
            for gid, g in ctx_o.groups.items():
                b = groups.get(gid)
                if base_cands is not None and gid in base_cands and b is not None and _routes_alike(H, g, b, ov):
                    cands_o[gid] = base_cands[gid]
                else:
                    cands_o[gid] = generate_candidates(H, g)
            crit_o = assess(H, ctx_o.groups, cands_o, base_likelihood=crit.base_likelihood or None)
            world = (ctx_o, cands_o, crit_o)
            if world_cache is not None:
                world_cache[key] = world
        return world

    def plan_world(ov):
        ctx_o, cands_o, crit_o = world_of(ov)
        xo = solve(DesignProblem(H, ctx_o.groups, cands_o, crit_o, phase=phase), order, eps, "o", use_cpsat=use_cpsat,
                   time_limit_s=time_limit_s)
        return ctx_o, crit_o, xo

    x0 = design
    if rebase and cands_e:
        _c, _k, x0 = plan_world(base)
    tasks: list[ReconTask] = []
    for eid, attr in sorted(cands_e.items()):
        el = H.elements[eid]
        needs = _needs(attr, groups)
        if not needs:
            continue
        classes = answer_classes(attr, needs)
        priors = answer_priors(H, eid, attr, classes)
        answers = []
        for (label, val), q in zip(classes, priors):
            ov = dict(base)
            for k in el.edges:
                ov[ukey(*k)] = {**ov.get(ukey(*k), {}), attr: val}
            ctx_o, cands_o, crit_o = world_of(ov)
            if all(gid in groups and _routes_alike(H, g, groups[gid], ov) for gid, g in ctx_o.groups.items()):
                # the answer leaves every group's roads as they are (a tunnel still too low for every vehicle): the plan
                # cannot change, and a new solve would only measure the solver. On Scenario 1, COA R credited 6 tunnels
                # with the same value for every answer, "too low for every vehicle" included.
                xo = x0
            else:
                xo = solve(DesignProblem(H, ctx_o.groups, cands_o, crit_o, phase=phase), order, eps, "o", use_cpsat=use_cpsat,
                           time_limit_s=time_limit_s)
            J1 = evaluate(H, ctx_o.groups, crit_o, replace(xo, recourse=_recourse(crit_o, xo.msr, xo.asrs, order)), phase)
            J0 = evaluate(H, ctx_o.groups, crit_o, replace(x0, recourse=_recourse(crit_o, x0.msr, x0.asrs, order)), phase)
            answers.append(Answer(label, q, val, J1, J0, {g: set(s) for g, s in crit_o.irreducible.items()},
                                  _design_changes(H, x0, xo, crit, crit_o)))
        values = {k: sum(a.prior * (a.baseline.get(k, 0.0) - a.objectives.get(k, 0.0)) for a in answers) for k in full}
        level = next((k for k in full if abs(values[k]) > ETA.get(k, 0.1)), None)
        removes = {}
        for gid in groups:
            p = sum(a.prior for a in answers if a.irreducible.get(gid, set()) < crit.irreducible.get(gid, set()))
            if p > 0:
                removes[gid] = round(p, 3)
        affected = sorted({g for a in answers for g, kind, _t in a.changes if kind in ("alternate", "spof")})
        deps = [groups[g].earliest for g in affected if g in groups and groups[g].earliest >= now]
        deadline = min(deps) if deps else None
        dist_m = point_distance(H.G, node_xy(H.G, asset), el.xy) if asset is not None else 0.0
        eta = now + RECCE_DETOUR * dist_m / 1000.0 / RECCE_SPEED_KMH * 60.0 + RECCE_TASK_MIN
        feasible = deadline is not None and eta <= deadline
        why = "; ".join(f"if {a.label}: {a.gains()}" for a in answers if a.gains()) or "no answer changes the plan"
        if level is not None:
            why += f". Value first appears on {level} ({values[level]:+.1f})"
        if removes:
            why += ". Probability of removing an irreducible single point of failure: " + ", ".join(f"{g} {p:.0%}" for g, p in removes.items())
        noun = {"bridge": "Classify", "tunnel": "Measure"}.get(el.kind, "Reconnoitre")
        question = {"mlc": f"{noun} {el.name}: load class, condition, clearance, approaches",
                    "max_height_m": f"{noun} {el.name}: clearance, width, condition"}.get(attr, f"{noun} {el.name}: {ATTRIBUTE_TEXT.get(attr, attr)}")
        tasks.append(ReconTask("", eid, el.name, attr, question, answers, level, values.get(level, 0.0) if level else 0.0, values,
                               removes, affected, eta, deadline, feasible, why))
    tasks.sort(key=lambda t: (not t.feasible, full.index(t.level) if t.level in full else len(full), -abs(t.value), t.element))
    for i, t in enumerate(tasks, start=1):
        t.id, t.priority = f"RT-{i}", i
    for eid in skipped:  # listed, not valued
        el = H.elements[eid]
        tasks.append(ReconTask(f"RT-{len(tasks) + 1}", eid, el.name, all_cands[eid],
                               f"Reconnoitre {el.name}: {ATTRIBUTE_TEXT.get(all_cands[eid], all_cands[eid])}", [], None, 0.0, {}, {}, [],
                               0.0, None, False, f"not valued: beyond the {max_elements} elements closest to the designated network"))
        tasks[-1].priority = len(tasks)
    return tasks


__all__ = ["Answer", "GroupResponse", "Playbook", "ReconTask", "answer_classes", "answer_priors", "build_playbooks",
           "group_response", "next_arc_tree", "playbook_diff", "recce_candidates", "tree_path", "value_of_information"]

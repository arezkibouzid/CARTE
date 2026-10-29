"""S7: movement credits (methodology 5, S7; Appendix B.2).

Every march unit drives its group's main route, cut into segments: the
corridors of the route, between decision points. A CP-SAT model chooses the
head's entry time into each segment, the waits (only at the start point and
at halt areas outside the PRO.CRITPOINT distance) and the order between units
on every shared segment:

C1 travel: entry into the next segment = exit from this one + halt + wait.
C2 same-direction time gap h (SCH.HEADWAY), reified on the order boolean o:
   o => t'_in >= t_in + pass + h  and  t'_in + tau' >= t_in + tau + pass + h
   (with constant speeds the gap is affine along the segment, so the two ends suffice).
C3 no overtaking between consecutive shared segments unless a halt area lies between them.
C4 single-lane segments (too narrow for the two columns to meet): opposite movements do not overlap.
C5 junctions: crossing movements do not pass a decision point at the same time (plus a clearance).
C6 halt areas: vehicles present never exceed the capacity (cumulative constraint).
C7 windows (SCH.WINDOW): no occupation of a built-up segment inside the window (T1) or as few as possible (T2).
C8 lateness against deadlines.

Objectives, in order: weighted lateness, window violations (T2), weighted delay
against the unconstrained arrival, waits outside start points, and in repair
the change of start-point times. Time is integer, in tenths of a minute.

Fallback (no OR-Tools, or no solution in time): priority-order scheduling with
the same time-gap rule, where a unit can only delay its departure.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field, replace
from typing import Any

from .candidates import Route, allowed_halt_nodes, halt_plan, make_route
from .corridors import Arc, CorridorGraph
from .graph import is_two_lane, path_edges
from .mission import Group, MarchUnit, MissionContext

TICK = 0.1  # minutes per integer time unit
JUNCTION_CLEARANCE_MIN = 0.5  # assumption, to confirm
MEET_MARGIN_M = 0.5  # two columns meet only if width >= width_a + width_b + margin (assumption)
HORIZON_EXTRA_MIN = 900.0


@dataclass
class Movement:
    """One segment (corridor) of a unit's route with the head's times."""

    arc: Arc
    start: Any
    end: Any
    t_in: float
    t_out: float
    pass_min: float  # minutes for the whole column to pass a point
    wait_before: float = 0.0
    halt_before: float = 0.0

    @property
    def tail_out(self) -> float:
        return self.t_out + self.pass_min


@dataclass
class Credit:
    """Movement credit of one march unit: route, start-point time, times at every decision point."""

    unit: str
    convoy: str
    group: str
    route: Route
    legs: list[Movement]
    earliest: float
    deadline: float | None
    weight: float
    n: int
    column_m: float
    free_arrival: float  # arrival without any other unit on the roads

    @property
    def depart(self) -> float:
        return self.legs[0].t_in if self.legs else self.earliest

    @property
    def arrive(self) -> float:
        return self.legs[-1].t_out if self.legs else self.earliest

    @property
    def hold(self) -> float:
        return max(0.0, self.depart - self.earliest)

    @property
    def lateness(self) -> float:
        return max(0.0, self.arrive - self.deadline) if self.deadline is not None else 0.0

    @property
    def delay(self) -> float:
        return max(0.0, self.arrive - self.free_arrival)

    def waits(self) -> list[tuple[Any, float, float]]:
        """(node, start, minutes) of waits outside the start point."""
        return [(l.start, l.t_in - l.wait_before, l.wait_before) for l in self.legs[1:] if l.wait_before > 1e-6]

    def halts(self) -> list[tuple[Any, float, float]]:
        return [(l.start, l.t_in - l.wait_before - l.halt_before, l.halt_before) for l in self.legs if l.halt_before > 1e-6]

    def time_at(self, node: Any) -> float | None:
        """Time the head leaves (or reaches, for the destination) a node of the route."""
        for l in self.legs:
            if l.start == node:
                return l.t_in
        if self.legs and self.legs[-1].end == node:
            return self.legs[-1].t_out
        return None

    def position(self, t: float) -> tuple[int, float]:
        """(index of the leg the head is on or about to enter, fraction driven) at time t."""
        for i, l in enumerate(self.legs):
            if t < l.t_in:
                return i, 0.0
            if t < l.t_out:
                return i, (t - l.t_in) / max(l.t_out - l.t_in, 1e-9)
        return len(self.legs), 1.0


@dataclass
class Schedule:
    credits: dict[str, Credit]
    solver: str
    status: str
    seconds: float
    levels: list[tuple[str, float]] = field(default_factory=list)
    order: dict[str, list[str]] = field(default_factory=dict)  # "K12+" -> units in driving order
    notes: list[str] = field(default_factory=list)

    def by_group(self, gid: str) -> list[Credit]:
        return [c for c in self.credits.values() if c.group == gid]


@dataclass
class Task:
    """A march unit to schedule on (the rest of) a route."""

    unit: MarchUnit
    group: Group
    route: Route  # full route, for the credit
    arcs: tuple[Arc, ...]  # arcs still to schedule (a suffix of the route)
    start_time: float
    history: list[Movement] = field(default_factory=list)
    halts: dict[Any, float] = field(default_factory=dict)  # node -> halt (min) before leaving it
    wait_nodes: set = field(default_factory=set)
    sp_target: float | None = None  # repair: previous start-point time
    taus: list[float] = field(default_factory=list)
    passes: list[float] = field(default_factory=list)
    free_arrival: float = 0.0


@dataclass
class Occupation:
    """A segment held by a unit at the time of a repair (fixed, others must respect it)."""

    arc: Arc
    unit: str
    t_in: float
    t_out: float
    pass_min: float
    blocks: bool = True  # False when the column pulled aside (two-lane road)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def column_length(group: Group, unit: MarchUnit) -> float:
    n = len(unit.vehicle_lengths)
    return sum(unit.vehicle_lengths) + max(0, n - 1) * group.coster.interval_m


def headway_min(group: Group) -> float:
    return float(group.active.param("SCH.HEADWAY", "headway_min", 0.0) or 0.0)


def _finish(task: Task) -> Task:
    co = task.group.coster
    task.taus = [co.arcs[a].time_min for a in task.arcs]
    task.passes = [co.pass_min(a, task.unit.vehicle_lengths) for a in task.arcs]
    task.free_arrival = task.start_time + sum(task.taus) + sum(task.halts.get(task.group.coster.H.arc_start(a), 0.0) for a in task.arcs[1:])
    return task


def _halts(H: CorridorGraph, group: Group, arcs: tuple[Arc, ...]) -> dict[Any, float]:
    max_drive = group.active.param("RTE.GAP_MAX", "max_drive_min", None)
    nodes = halt_plan(H, group.coster, arcs, max_drive, allowed_halt_nodes(H, group)) or []
    dur = float(group.active.param("SCH.HALT", "halt_min", 15.0) or 15.0)
    return {n: dur for n in nodes}


def make_tasks(H: CorridorGraph, ctx: MissionContext, routes: dict[str, Route]) -> list[Task]:
    """One task per march unit on its group's route (initial plan)."""
    tasks = []
    for uid, u in ctx.units.items():
        g = ctx.groups[u.group_id]
        r = routes.get(g.id)
        if r is None or not r.arcs:
            continue
        halts = {n: float(g.active.param("SCH.HALT", "halt_min", 15.0) or 15.0) for n in (r.halts or [])}
        tasks.append(_finish(Task(u, g, r, r.arcs, u.earliest, [], halts, allowed_halt_nodes(H, g))))
    return tasks


def replan_tasks(
    H: CorridorGraph,
    ctx: MissionContext,
    prev: Schedule,
    now: float,
    new_routes: dict[str, Route] | None = None,
    delays: dict[str, float] | None = None,
    pulled_aside: set[str] | None = None,
    turn_back: dict[str, float] | None = None,
) -> tuple[list[Task], list[Occupation], dict[str, Credit]]:
    """State of every unit at ``now``, ready for a repair schedule.

    Arrived units are kept. A moving unit keeps the legs it has driven and the
    leg it is on, then continues from the next decision point; that leg becomes
    a fixed occupation for the others. A unit not yet departed restarts from its
    start point. ``new_routes`` maps a unit to its new route from the point where
    it restarts; ``delays`` adds a stop (breakdown) to a unit's next leg;
    ``turn_back`` (unit -> turn-around minutes) sends a unit back to the start of
    the leg it is on (a lost element ahead), where it restarts."""
    new_routes = new_routes or {}
    delays = delays or {}
    pulled_aside = pulled_aside or set()
    turn_back = turn_back or {}
    tasks: list[Task] = []
    fixed: list[Occupation] = []
    kept: dict[str, Credit] = {}
    for uid, c in prev.credits.items():
        u = ctx.units[uid]
        g = ctx.groups[u.group_id]
        delay = delays.get(uid, 0.0)
        if c.legs and c.arrive <= now:
            kept[uid] = c
            continue
        if not c.legs or c.depart >= now:
            r = new_routes.get(uid, c.route)
            if not r.arcs:
                continue
            tasks.append(_finish(Task(u, g, r, r.arcs, max(now, c.earliest) + delay, [], _halts(H, g, r.arcs),
                                      allowed_halt_nodes(H, g), sp_target=c.depart)))
            continue
        i = max(j for j, l in enumerate(c.legs) if l.t_in <= now)
        cur = c.legs[i]
        history = [replace(l) for l in c.legs[: i + 1]]
        history[-1].t_out = max(cur.t_out, now) + delay if delay else cur.t_out
        node = cur.end
        turned = uid in turn_back and now < cur.t_out
        if turned:
            history[-1].end = cur.start  # drives back to the decision point it came from
            history[-1].t_out = now + (now - cur.t_in) + turn_back[uid] + delay
            node = cur.start
        fixed.append(Occupation(cur.arc, uid, cur.t_in, history[-1].t_out, cur.pass_min, blocks=uid not in pulled_aside))
        rest = new_routes.get(uid)
        if turned and rest is None:
            rest_arcs: tuple = ()  # no route given: the unit waits at the decision point it returned to
        else:
            rest_arcs = rest.arcs if rest is not None else tuple(l.arc for l in c.legs[i + 1:])
        full_arcs = tuple(l.arc for l in (history[:-1] if turned else history)) + tuple(rest_arcs)
        full = make_route(H, g, full_arcs, set(), allowed_halt_nodes(H, g)) if (rest is not None or turned) and full_arcs else c.route
        if not rest_arcs:
            kept[uid] = Credit(uid, u.convoy_id, g.id, full, history, c.earliest, c.deadline, c.weight, c.n, c.column_m,
                               c.free_arrival)
            continue
        t = Task(u, g, full, tuple(rest_arcs), history[-1].t_out, history, _halts(H, g, tuple(rest_arcs)),
                 allowed_halt_nodes(H, g) | {node})
        _finish(t)
        t.free_arrival = c.free_arrival + delay  # delay against the original plan, plus the breakdown
        tasks.append(t)
    return tasks, fixed, kept


def can_meet(H: CorridorGraph, cid: str, width_a: float, width_b: float, margin: float = MEET_MARGIN_M) -> bool:
    """Can two columns meet on every road of the corridor? Unknown width counts as no."""
    G = H.G
    for u, v in path_edges(H.corridors[cid].nodes):
        d = G[u][v] if G.has_edge(u, v) else G[v][u]
        if d.get("lanes") is not None and d.get("width_m") is None:
            if d["lanes"] < 2:
                return False
            continue
        w = d.get("width_m")
        if w is None or w < width_a + width_b + margin:
            return False
    return True


def can_pull_aside(H: CorridorGraph, arc: Arc, width: float) -> bool:
    """Can a column stop on this segment without blocking it (two-lane road for its width)?"""
    G = H.G
    for u, v in H.arc_edges(arc):
        if is_two_lane(G[u][v], width, MEET_MARGIN_M) is not True:
            return False
    return True


# ---------------------------------------------------------------------------
# Solvers
# ---------------------------------------------------------------------------


def schedule(
    H: CorridorGraph,
    ctx: MissionContext,
    routes: dict[str, Route] | None = None,
    tasks: list[Task] | None = None,
    fixed: list[Occupation] | None = None,
    kept: dict[str, Credit] | None = None,
    time_limit_s: float = 10.0,
    workers: int = 1,
    use_cpsat: bool = True,
    repair: bool = False,
) -> Schedule:
    """Movement credits for every march unit (CP-SAT, greedy fallback)."""
    t0 = time.perf_counter()
    if tasks is None:
        tasks = make_tasks(H, ctx, routes or {})
    fixed = fixed or []
    kept = dict(kept or {})
    sched = None
    if use_cpsat and tasks:
        try:
            sched = _solve_cpsat(H, tasks, fixed, time_limit_s, workers, repair)
        except ImportError:
            sched = None
    if sched is None:
        sched = _greedy(H, tasks, fixed)
    sched.credits = {**kept, **sched.credits}
    sched.credits = dict(sorted(sched.credits.items()))
    sched.seconds = time.perf_counter() - t0
    return sched


def _ticks(minutes: float) -> int:
    return int(round(minutes / TICK))


def _legs_ticks(task: Task) -> tuple[list[int], list[int]]:
    """Leg durations from rounded cumulative times, so totals stay exact to half a tick."""
    cum = [0.0]
    for x in task.taus:
        cum.append(cum[-1] + x)
    d = [_ticks(cum[i + 1]) - _ticks(cum[i]) for i in range(len(task.taus))]
    return d, [max(1, _ticks(p)) for p in task.passes]


def _windows(task: Task) -> list[tuple[str, str, float, float]]:
    return [(r.id, r.tier, float(r.p("start_min")), float(r.p("end_min"))) for r in task.group.active.of("SCH.WINDOW") if r.p("area") == "built_up"]


def _builtup_arc(H: CorridorGraph, task: Task, arc: Arc) -> bool:
    return any(task.group.coster.evals[e].builtup for e in H.arc_edges(arc))


def _solve_cpsat(H: CorridorGraph, tasks: list[Task], fixed: list[Occupation], time_limit_s: float, workers: int, repair: bool) -> Schedule | None:
    from ortools.sat.python import cp_model

    m = cp_model.CpModel()
    K = range(len(tasks))
    D, P = {}, {}
    for k in K:
        D[k], P[k] = _legs_ticks(tasks[k])
    longest = max(sum(D[k]) for k in K)
    horizon = _ticks(max(t.start_time for t in tasks) + HORIZON_EXTRA_MIN) + 3 * longest
    tv: dict[tuple[int, int], Any] = {}
    wv: dict[tuple[int, int], Any] = {}
    arrival = {}
    for k in K:
        tk = tasks[k]
        n = len(tk.arcs)
        for i in range(n):
            tv[k, i] = m.NewIntVar(0, horizon, f"t_{k}_{i}")
        m.Add(tv[k, 0] >= _ticks(tk.start_time))
        for i in range(1, n):
            node = H.arc_start(tk.arcs[i])
            halt = _ticks(tk.halts.get(node, 0.0))
            cap = H.G.nodes[node].get("halt_capacity")
            if node in tk.wait_nodes and (not cap or tk.unit.n <= cap):
                wv[k, i] = m.NewIntVar(0, horizon, f"w_{k}_{i}")
                m.Add(tv[k, i] == tv[k, i - 1] + D[k][i - 1] + halt + wv[k, i])  # C1
            else:
                m.Add(tv[k, i] == tv[k, i - 1] + D[k][i - 1] + halt)
        arrival[k] = tv[k, n - 1] + D[k][n - 1]

    def h_of(k, k2):
        return _ticks(max(headway_min(tasks[k].group), headway_min(tasks[k2].group)))

    use: dict[Arc, list[tuple[int, int]]] = {}
    for k in K:
        for i, a in enumerate(tasks[k].arcs):
            use.setdefault(a, []).append((k, i))
    order: dict[tuple[Arc, int, int], Any] = {}
    for a, lst in use.items():  # C2
        for (k, i), (k2, j) in itertools.combinations(lst, 2):
            if k == k2:
                continue
            h = h_of(k, k2)
            o = m.NewBoolVar(f"o_{a[0]}{a[1]}_{k}_{k2}")
            order[a, k, k2] = o
            m.Add(tv[k2, j] >= tv[k, i] + P[k][i] + h).OnlyEnforceIf(o)
            m.Add(tv[k2, j] + D[k2][j] >= tv[k, i] + D[k][i] + P[k][i] + h).OnlyEnforceIf(o)
            m.Add(tv[k, i] >= tv[k2, j] + P[k2][j] + h).OnlyEnforceIf(o.Not())
            m.Add(tv[k, i] + D[k][i] >= tv[k2, j] + D[k2][j] + P[k2][j] + h).OnlyEnforceIf(o.Not())
    for k in K:  # C3
        arcs = tasks[k].arcs
        for i in range(len(arcs) - 1):
            a, b = arcs[i], arcs[i + 1]
            node = H.arc_end(a)
            if H.G.nodes[node].get("halt_capacity"):
                continue
            for k2 in K:
                if k2 <= k:
                    continue
                if (a, k, k2) in order and (b, k, k2) in order:
                    i2 = next((j for j, x in enumerate(tasks[k2].arcs[:-1]) if x == a and tasks[k2].arcs[j + 1] == b), None)
                    if i2 is not None:
                        m.Add(order[a, k, k2] == order[b, k, k2])
    for cid in {a[0] for a in use}:  # C4
        fw, bw = use.get((cid, 1), []), use.get((cid, -1), [])
        for (k, i), (k2, j) in itertools.product(fw, bw):
            if k == k2 or can_meet(H, cid, tasks[k].group.profile.width_m, tasks[k2].group.profile.width_m):
                continue
            b = m.NewBoolVar(f"m_{cid}_{k}_{k2}")
            m.Add(tv[k2, j] >= tv[k, i] + D[k][i] + P[k][i]).OnlyEnforceIf(b)
            m.Add(tv[k, i] >= tv[k2, j] + D[k2][j] + P[k2][j]).OnlyEnforceIf(b.Not())
    passages: dict[Any, list[tuple[int, int, Arc | None, Arc]]] = {}  # C5
    for k in K:
        arcs = tasks[k].arcs
        for i, a in enumerate(arcs):
            passages.setdefault(H.arc_start(a), []).append((k, i, arcs[i - 1] if i else None, a))
    clear = _ticks(JUNCTION_CLEARANCE_MIN)
    for v, lst in passages.items():
        for (k, i, ain, aout), (k2, j, bin_, bout) in itertools.combinations(lst, 2):
            if k == k2 or (ain is not None and ain == bin_) or aout == bout:
                continue
            if ain is not None and bin_ is not None and bin_ == (aout[0], -aout[1]) and bout == (ain[0], -ain[1]):
                continue  # the two columns meet on the same road; C4 covers single-lane roads
            b = m.NewBoolVar(f"j_{k}_{k2}_{i}_{j}")
            m.Add(tv[k2, j] >= tv[k, i] + P[k][i] + clear).OnlyEnforceIf(b)
            m.Add(tv[k, i] >= tv[k2, j] + P[k2][j] + clear).OnlyEnforceIf(b.Not())
    notes: list[str] = []
    halt_use: dict[Any, list[tuple[Any, int]]] = {}  # C6
    for k in K:
        tk = tasks[k]
        for i in range(1, len(tk.arcs)):
            node = H.arc_start(tk.arcs[i])
            cap = H.G.nodes[node].get("halt_capacity")
            if not cap or (node not in tk.halts and (k, i) not in wv):
                continue
            if tk.unit.n > cap:
                notes.append(f"{tk.unit.id}: halt at {H.label(node)} needs {tk.unit.n} places, capacity {cap}")
                continue
            start = tv[k, i - 1] + D[k][i - 1]
            size = m.NewIntVar(0, horizon, f"hs_{k}_{i}")
            m.Add(size == tv[k, i] - start)
            iv = m.NewIntervalVar(start, size, tv[k, i], f"halt_{k}_{i}")
            halt_use.setdefault(node, []).append((iv, tk.unit.n))
    for node, lst in halt_use.items():
        if len(lst) > 1:
            m.AddCumulative([iv for iv, _ in lst], [n for _, n in lst], int(H.G.nodes[node]["halt_capacity"]))
    viol = []  # C7
    for k in K:
        tk = tasks[k]
        for rid, tier, s, e in _windows(tk):
            for i, a in enumerate(tk.arcs):
                if not _builtup_arc(H, tk, a):
                    continue
                before, after = m.NewBoolVar(""), m.NewBoolVar("")
                m.Add(tv[k, i] + D[k][i] + P[k][i] <= _ticks(s)).OnlyEnforceIf(before)
                m.Add(tv[k, i] >= _ticks(e)).OnlyEnforceIf(after)
                if tier == "T1":
                    m.AddBoolOr([before, after])
                else:
                    v = m.NewBoolVar("")
                    m.AddBoolOr([before, after, v])
                    viol.append((int(round(tk.unit.weight)), v))
    for occ in fixed:  # repair: segments held by moving units
        for k in K:
            for i, a in enumerate(tasks[k].arcs):
                if occ.unit == tasks[k].unit.id:
                    continue
                if a == occ.arc:
                    h = _ticks(headway_min(tasks[k].group))
                    m.Add(tv[k, i] >= _ticks(occ.t_in + occ.pass_min) + h)
                    if occ.blocks:
                        m.Add(tv[k, i] + D[k][i] >= _ticks(occ.t_out + occ.pass_min) + h)
                elif a == (occ.arc[0], -occ.arc[1]) and not can_meet(H, a[0], tasks[k].group.profile.width_m, tasks[k].group.profile.width_m):
                    m.Add(tv[k, i] >= _ticks(occ.t_out + occ.pass_min))
    lat = {}
    for k in K:  # C8
        dl = tasks[k].unit.deadline
        if dl is not None:
            lat[k] = m.NewIntVar(0, horizon, f"lat_{k}")
            m.Add(lat[k] >= arrival[k] - _ticks(dl))
    w = {k: int(round(tasks[k].unit.weight)) for k in K}
    exprs: list[tuple[str, Any]] = [("lateness", sum(w[k] * lat[k] for k in lat) if lat else None)]
    if viol:
        exprs.append(("window", sum(c * v for c, v in viol)))
    exprs.append(("delay", sum(w[k] * (arrival[k] - _ticks(tasks[k].free_arrival)) for k in K)))
    waits = [wv[k, i] for (k, i) in wv]
    exprs.append(("waits", sum(waits) if waits else None))
    if repair:
        stab = []
        for k in K:
            if tasks[k].sp_target is not None:
                dv = m.NewIntVar(0, horizon, f"d_{k}")
                m.AddAbsEquality(dv, tv[k, 0] - _ticks(tasks[k].sp_target))
                stab.append(dv)
        exprs.append(("stability", sum(stab) if stab else None))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = 0
    levels = []
    exact = True
    first = True
    for name, e in exprs + [("tie-break", sum(tv[k, 0] for k in K))]:
        if e is None:
            continue
        if not first:
            m.ClearHints()
            for var in list(tv.values()) + list(wv.values()):
                m.AddHint(var, solver.Value(var))
        m.Minimize(e)
        st = solver.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if first:
                return None
            exact = False
            break
        first = False
        exact = exact and st == cp_model.OPTIMAL
        best = int(round(solver.ObjectiveValue()))
        m.Add(e <= best)
        if name != "tie-break":
            levels.append((name, best * TICK if name != "window" else float(best)))
    if first:
        return None
    credits = {}
    for k in K:
        tk = tasks[k]
        legs = list(tk.history)
        prev_end = None
        for i, a in enumerate(tk.arcs):
            t_in = solver.Value(tv[k, i]) * TICK
            node = H.arc_start(a)
            halt = tk.halts.get(node, 0.0) if i else 0.0
            wait = solver.Value(wv[k, i]) * TICK if (k, i) in wv else (t_in - tk.start_time if i == 0 else 0.0)
            if i and prev_end is not None:
                wait = max(0.0, t_in - prev_end - halt)
            t_out = t_in + D[k][i] * TICK
            legs.append(Movement(a, node, H.arc_end(a), t_in, t_out, tk.passes[i], wait, halt))
            prev_end = t_out
        credits[tk.unit.id] = _credit(tk, legs)
    out = {}
    for (a, k, k2), o in order.items():
        first_k, second = (k, k2) if solver.Value(o) else (k2, k)
        key = f"{a[0]}{'+' if a[1] > 0 else '-'}"
        lst = out.setdefault(key, [])
        for x in (tasks[first_k].unit.id, tasks[second].unit.id):
            if x not in lst:
                lst.append(x)
    return Schedule(credits, "cp-sat", "optimal" if exact else "feasible (time limit)", 0.0, levels, out, notes)


def _credit(tk: Task, legs: list[Movement]) -> Credit:
    u = tk.unit
    return Credit(u.id, u.convoy_id, tk.group.id, tk.route, legs, u.earliest, u.deadline, u.weight, u.n,
                  column_length(tk.group, u), tk.free_arrival)


def _greedy(H: CorridorGraph, tasks: list[Task], fixed: list[Occupation]) -> Schedule:
    """Priority order; each unit leaves as early as the time-gap rule allows behind the units already placed."""
    order = sorted(range(len(tasks)), key=lambda k: (-tasks[k].unit.weight, tasks[k].start_time, tasks[k].unit.id))
    placed: list[tuple[Task, list[Movement]]] = []
    notes = []
    credits = {}

    def legs_at(tk: Task, dep: float) -> list[Movement]:
        out = []
        t = dep
        for i, a in enumerate(tk.arcs):
            node = H.arc_start(a)
            halt = tk.halts.get(node, 0.0) if i else 0.0
            t += halt
            out.append(Movement(a, node, H.arc_end(a), t, t + tk.taus[i], tk.passes[i], dep - tk.start_time if i == 0 else 0.0, halt))
            t += tk.taus[i]
        return out

    def shift_needed(tk: Task, legs: list[Movement]) -> float:
        need = 0.0
        h_me = headway_min(tk.group)
        for other, olegs in placed:
            h = max(h_me, headway_min(other.group))
            for l in legs:
                for o in olegs:
                    if o.arc == l.arc:
                        after_ok = l.t_in >= o.t_in + o.pass_min + h - 1e-6 and l.t_out >= o.t_out + o.pass_min + h - 1e-6
                        before_ok = o.t_in >= l.t_in + l.pass_min + h - 1e-6 and o.t_out >= l.t_out + l.pass_min + h - 1e-6
                        if not (after_ok or before_ok):
                            need = max(need, o.t_in + o.pass_min + h - l.t_in, o.t_out + o.pass_min + h - l.t_out)
                    elif o.arc == (l.arc[0], -l.arc[1]) and not can_meet(H, l.arc[0], tk.group.profile.width_m, other.group.profile.width_m):
                        if l.t_in < o.t_out + o.pass_min and o.t_in < l.t_out + l.pass_min:
                            need = max(need, o.t_out + o.pass_min - l.t_in)
        for occ in fixed:
            if occ.unit == tk.unit.id:
                continue
            for l in legs:
                if l.arc == occ.arc:
                    h = h_me
                    need = max(need, occ.t_in + occ.pass_min + h - l.t_in, (occ.t_out + occ.pass_min + h - l.t_out) if occ.blocks else 0.0)
        for rid, tier, s, e in _windows(tk):
            if tier != "T1":
                continue
            for l in legs:
                if _builtup_arc(H, tk, l.arc) and l.t_in < e and s < l.tail_out:
                    need = max(need, e - l.t_in)
        return need

    for k in order:
        tk = tasks[k]
        dep = tk.start_time
        legs = legs_at(tk, dep)
        for _ in range(1000):
            s = shift_needed(tk, legs)
            if s <= 1e-6:
                break
            dep += max(s, TICK)
            legs = legs_at(tk, dep)
        else:
            notes.append(f"{tk.unit.id}: no conflict-free departure found by the greedy scheduler")
        placed.append((tk, legs))
        credits[tk.unit.id] = _credit(tk, list(tk.history) + legs)
    lat = sum(c.weight * c.lateness for c in credits.values())
    delay = sum(c.weight * c.delay for c in credits.values())
    return Schedule(credits, "greedy", "heuristic", 0.0, [("lateness", lat), ("delay", delay)], {}, notes)


# ---------------------------------------------------------------------------
# March tables and road movement graph data
# ---------------------------------------------------------------------------


def hhmm(t: float | None) -> str:
    """Clock time HH:MM of a time in minutes from 00:00 (rounded to the minute)."""
    if t is None:
        return "-"
    h, mnt = divmod(int(round(t)), 60)
    return f"{h % 24:02d}:{mnt:02d}"


def march_table(H: CorridorGraph, sched: Schedule, ref=None) -> list[dict]:
    """One row per march unit and control point: SP, control posts, halts, RP (times of head and tail)."""
    rows = []
    for uid, c in sched.credits.items():
        posts = {p.node: p.id for p in ref.posts} if ref is not None else {}
        route_name = ref.name_of(c.route) if ref is not None else None
        points = []
        for i, l in enumerate(c.legs):
            if i == 0:
                points.append(("SP", l.start, l.t_in, l.t_in + l.pass_min))
            elif l.start in posts:
                points.append((posts[l.start], l.start, l.t_in, l.t_in + l.pass_min))
            if l.halt_before or (i and l.wait_before):
                points.append(("halt" if l.halt_before else "wait", l.start, l.t_in - l.halt_before - l.wait_before, l.t_in))
        if c.legs:
            last = c.legs[-1]
            points.append(("RP", last.end, last.t_out, last.t_out + last.pass_min))
        for kind, node, t_head, t_tail in points:
            rows.append({
                "unit": uid,
                "convoy": c.convoy,
                "route": route_name or "-",
                "point": kind,
                "place": H.label(node),
                "head": hhmm(t_head),
                "tail": hhmm(t_tail),
                "deadline": hhmm(c.deadline) if kind == "RP" and c.deadline is not None else "",
            })
    return rows


def movement_lines(H: CorridorGraph, credit: Credit, along: Route) -> list[tuple[float, float, float]]:
    """(time, head distance, tail distance) points of a unit on the legs it shares with ``along``."""
    from .refine import chainage

    pos = chainage(H, along)
    out = []
    for l in credit.legs:
        if l.start in pos and l.end in pos and pos[l.end] > pos[l.start]:
            out.append((l.t_in, pos[l.start], l.pass_min))
            out.append((l.t_out, pos[l.end], l.pass_min))
    return out


__all__ = [
    "Credit",
    "Movement",
    "Occupation",
    "Schedule",
    "TICK",
    "Task",
    "can_meet",
    "can_pull_aside",
    "column_length",
    "headway_min",
    "hhmm",
    "make_tasks",
    "march_table",
    "movement_lines",
    "replan_tasks",
    "schedule",
]

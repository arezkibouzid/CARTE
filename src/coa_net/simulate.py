"""S8 (simulation): discrete-event replay and Monte Carlo of a movement plan (methodology S8; Appendix B.3).

Columns move segment by segment (corridors between decision points):

* travel time: the group's segment time times a lognormal factor (sigma 0.1)
  drawn per (replication, unit, segment);
* following: a column never closes within the headway of the column ahead on
  the same segment (time gap at both ends, as in the scheduler); it slows down
  instead of overtaking;
* single-lane segments: a column waits until an opposite column has cleared;
* breakdowns: a rate per vehicle.km; on a two-lane road the vehicle pulls
  aside and the column goes on, otherwise the column stops for the repair time
  (10-40 min) and blocks the columns behind it; scripted breakdowns stop the
  whole column for the stated time;
* movement credits: a unit leaves its start point at its credit time, keeps
  its planned halts and waits, and drives freely otherwise;
* knowledge: a loss is known to a unit through orders, d_r + computation +
  d_o after the event (report 2-15 min, orders 2-10 min), or through the
  playbook trigger, d_r + d_b (broadcast 1-3 min), when a playbook covers the
  element; a unit that reaches a lost element first turns back (10 min);
* response: the surviving designated alternate when the column is on it,
  otherwise the shortest-path tree on the roads minus the known losses;
* exposure: vehicle.minutes on roads whose threat level is at least 0.3;
* conflicts: waits or slow-downs forced by another column, zero when the
  deterministic replay follows conflict-free movement credits.

Common random numbers: every random draw comes from a stream keyed by
(seed, replication, source, unit, segment), so every plan and pipeline sees
the same speeds, delays and breakdowns. Threat reports do not re-route
columns here; the incident loop does that.
"""

from __future__ import annotations

import heapq
import itertools
import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

from .anticipation import next_arc_tree
from .candidates import Route
from .corridors import Arc, CorridorGraph
from .graph import edge_midpoint, edges_within, point_distance
from .mission import MissionContext
from .schedule import TICK, Credit, Schedule, can_meet, can_pull_aside, headway_min

INF = float("inf")


@dataclass
class SimParams:
    speed_sigma: float = 0.1
    breakdown_rate: float = 0.002  # per vehicle.km
    repair_min: tuple[float, float] = (10.0, 40.0)
    report_min: tuple[float, float] = (2.0, 15.0)  # d_r
    order_min: tuple[float, float] = (2.0, 10.0)  # d_o
    broadcast_min: tuple[float, float] = (1.0, 3.0)  # d_b
    compute_min: float = 1.0
    turn_around_min: float = 10.0
    threat_level: float = 0.3
    horizon_min: float = 600.0
    unprepared_factor: float = 1.0  # time factor on roads outside the designated network after a re-route


@dataclass
class SimIncident:
    kind: str  # closure | threat | breakdown
    time: float
    element: str | None = None
    corridor: str | None = None
    center: tuple[float, float] | None = None
    radius: float = 0.0
    severity: float = 0.0
    unit: str | None = None
    duration: float = 0.0
    label: str = ""


@dataclass
class SimPlan:
    """What a pipeline hands over to execution."""

    name: str
    routes: dict[str, Route]  # unit -> route
    credits: dict[str, Credit] = field(default_factory=dict)
    designated: set[str] = field(default_factory=set)  # corridor ids
    alternates: dict[str, list[Route]] = field(default_factory=dict)  # group -> designated alternates
    playbooks: set[str] = field(default_factory=set)  # elements covered by a briefed playbook


@dataclass
class UnitOutcome:
    unit: str
    arrival: float | None
    planned: float
    deadline: float | None
    exposure: float  # vehicle.min in threat areas
    reached_closed: int
    rerouted: bool
    path: list[tuple[Arc, float, float]] = field(default_factory=list)

    @property
    def on_time(self) -> bool:
        return self.arrival is not None and (self.deadline is None or self.arrival <= self.deadline + 1e-6)

    @property
    def delay(self) -> float:
        return (self.arrival - self.planned) if self.arrival is not None else INF


@dataclass
class RepOutcome:
    rep: int
    units: dict[str, UnitOutcome]
    script: list[SimIncident]
    conflicts: int = 0  # waits or slow-downs forced by another column (zero in the deterministic replay of a plan)

    @property
    def completed(self) -> bool:
        return all(u.arrival is not None for u in self.units.values())


@dataclass
class SimResult:
    plan: str
    reps: list[RepOutcome]
    weights: dict[str, float]

    def on_time(self, uid: str | None = None) -> float:
        xs = [u.on_time for r in self.reps for k, u in r.units.items() if uid is None or k == uid]
        return sum(xs) / len(xs) if xs else 0.0

    def completion(self) -> float:
        return sum(r.completed for r in self.reps) / len(self.reps) if self.reps else 0.0

    def weighted_delay(self, rep: RepOutcome, cap: float = 600.0) -> float:
        return sum(self.weights.get(k, 1.0) * min(max(0.0, u.delay), cap) for k, u in rep.units.items())

    def mean_delay(self, uid: str | None = None, cap: float = 600.0) -> float:
        xs = [min(max(0.0, u.delay), cap) for r in self.reps for k, u in r.units.items() if uid is None or k == uid]
        return statistics.fmean(xs) if xs else 0.0

    def exposure(self) -> float:
        return statistics.fmean(sum(u.exposure for u in r.units.values()) for r in self.reps) if self.reps else 0.0

    def reached_closed(self) -> float:
        return sum(any(u.reached_closed for u in r.units.values()) for r in self.reps) / len(self.reps) if self.reps else 0.0

    def summary(self) -> dict[str, Any]:
        units = sorted({k for r in self.reps for k in r.units})
        return {
            "plan": self.plan,
            "replications": len(self.reps),
            "on_time": {u: round(self.on_time(u), 3) for u in units},
            "mean_delay_min": {u: round(self.mean_delay(u), 1) for u in units},
            "completion": round(self.completion(), 3),
            "weighted_delay": round(statistics.fmean(self.weighted_delay(r) for r in self.reps), 1) if self.reps else 0.0,
            "exposure_veh_min": round(self.exposure(), 1),
            "reached_closed": round(self.reached_closed(), 3),
            "conflicts_per_rep": round(statistics.fmean(r.conflicts for r in self.reps), 2) if self.reps else 0.0,
        }


def plan_from(H: CorridorGraph, ctx: MissionContext, design, sched: Schedule | None, playbooks=None, name: str = "COA-Net") -> SimPlan:
    routes = {}
    for uid, u in ctx.units.items():
        if sched is not None and uid in sched.credits:
            routes[uid] = sched.credits[uid].route
        elif u.group_id in design.msr:
            routes[uid] = design.msr[u.group_id]
    return SimPlan(name, routes, dict(sched.credits) if sched is not None else {}, set(design.designated),
                   {g: list(a) for g, a in design.asrs.items()}, {pb.element for pb in (playbooks or [])})


def _rng(*key) -> random.Random:
    return random.Random("|".join(str(k) for k in key))


def _uniform(rng: random.Random, lo_hi: tuple[float, float], deterministic: bool) -> float:
    lo, hi = lo_hi
    return (lo + hi) / 2.0 if deterministic else rng.uniform(lo, hi)


@dataclass
class _State:
    uid: str
    node: Any
    route: tuple[Arc, ...]
    i: int = 0
    off_plan: bool = False
    known: set[str] = field(default_factory=set)
    exposure: float = 0.0
    reached: int = 0
    arrived: float | None = None
    path: list[tuple[Arc, float, float]] = field(default_factory=list)
    broken: set[int] = field(default_factory=set)  # scripted breakdowns already applied


def run_once(H: CorridorGraph, ctx: MissionContext, plan: SimPlan, script: list[SimIncident], rep: int = 0, seed: int = 0,
             params: SimParams | None = None, deterministic: bool = False) -> RepOutcome:
    P = params or SimParams()
    G = H.G
    closures = [(k, inc) for k, inc in enumerate(script) if inc.kind == "closure"]
    threats = [(inc, set(edges_within(G, inc.center, inc.radius))) for inc in script if inc.kind == "threat" and inc.center]
    breakdowns = [(k, inc) for k, inc in enumerate(script) if inc.kind == "breakdown"]
    closed_at: dict[str, float] = {}
    closed_corr: dict[str, float] = {}
    for _, inc in closures:
        if inc.element:
            closed_at[inc.element] = min(closed_at.get(inc.element, INF), inc.time)
        if inc.corridor:
            closed_corr[inc.corridor] = min(closed_corr.get(inc.corridor, INF), inc.time)

    states: dict[str, _State] = {}
    know: dict[str, dict[str, float]] = {}
    heap: list[tuple[float, int, str]] = []
    tick = itertools.count()
    for uid, route in plan.routes.items():
        if not route.arcs:
            continue
        u = ctx.units[uid]
        c = plan.credits.get(uid)
        start = c.depart if c is not None else u.earliest
        states[uid] = _State(uid, H.arc_start(route.arcs[0]), tuple(route.arcs))
        know[uid] = {}
        for k, inc in closures:
            key = inc.element or f"corridor:{inc.corridor}"
            d_r = _uniform(_rng(seed, rep, "report", k), P.report_min, deterministic)
            if inc.element in plan.playbooks:
                lag = d_r + _uniform(_rng(seed, rep, "broadcast", k, uid), P.broadcast_min, deterministic)
            else:
                lag = d_r + P.compute_min + _uniform(_rng(seed, rep, "orders", k, uid), P.order_min, deterministic)
            know[uid][key] = min(know[uid].get(key, INF), inc.time + lag)
        heapq.heappush(heap, (start, next(tick), uid))

    occ_same: dict[Arc, list[tuple[float, float, float, str]]] = {}  # arc -> (t_in, t_out_block, pass, unit)
    occ_opp: dict[str, list[tuple[int, float, float]]] = {}  # corridor -> (dirn, t_in, clear)
    trees: dict[tuple, dict] = {}
    conflicts = 0  # times a column had to wait or slow down for another

    def known_closed(st: _State, t: float) -> tuple[set[str], set[str]]:
        els, cors = set(), set()
        for key, ta in know[st.uid].items():
            if ta <= t or key in st.known:
                if key.startswith("corridor:"):
                    cors.add(key.split(":", 1)[1])
                else:
                    els.add(key)
        return els, cors

    def arc_hits(arc: Arc, els: set[str], cors: set[str]) -> bool:
        return arc[0] in cors or bool(set(H.corridors[arc[0]].elements) & els)

    def policy(st: _State, node: Any, els: set[str], cors: set[str]) -> Arc | None:
        g = ctx.groups[ctx.units[st.uid].group_id]
        for alt in plan.alternates.get(g.id, []):
            if not any(arc_hits(a, els, cors) for a in alt.arcs):
                for j, a in enumerate(alt.arcs):
                    if H.arc_start(a) == node:
                        return a
        key = (g.id, frozenset(els), frozenset(cors))
        if key not in trees:
            trees[key] = next_arc_tree(H, g, els, cors)
        t = trees[key].get(node)
        return t[0] if t else None

    def threat_at(u: Any, v: Any, t: float) -> float:
        d = G[u][v]
        lvl = d.get("threat", 0.0)
        for inc, edges in threats:
            if inc.time <= t and (u, v) in edges:
                dist = point_distance(G, edge_midpoint(G, u, v), inc.center)
                lvl = max(lvl, inc.severity * (1.0 - 0.5 * dist / max(inc.radius, 1.0)))
        return lvl

    while heap:
        t, _, uid = heapq.heappop(heap)
        st = states[uid]
        unit = ctx.units[uid]
        g = ctx.groups[unit.group_id]
        if st.node == g.destination and (st.off_plan or st.i >= len(st.route)):
            st.arrived = t
            continue
        if t > g.earliest + P.horizon_min:
            continue  # did not arrive within the horizon
        els, cors = known_closed(st, t)
        if not st.off_plan and any(arc_hits(a, els, cors) for a in st.route[st.i:]):
            st.off_plan = True
        if st.off_plan:
            arc = policy(st, st.node, els, cors)
            if arc is None:
                continue  # stranded: no known route left
        else:
            arc = st.route[st.i]
        earliest = t
        c = plan.credits.get(uid)
        if not st.off_plan and c is not None and st.i < len(c.legs) and c.legs[st.i].arc == arc:
            leg = c.legs[st.i]
            earliest += leg.halt_before
            if st.i == 0 or leg.wait_before > 1e-6:
                earliest = max(earliest, leg.t_in)
        for k, inc in breakdowns:  # scripted breakdown while waiting at a decision point or before departure
            if inc.unit == uid and k not in st.broken and inc.time <= earliest:
                st.broken.add(k)
                earliest = max(earliest, inc.time) + inc.duration
        ae = g.coster.arcs[arc]
        factor = 1.0 if deterministic else math.exp(_rng(seed, rep, "speed", uid, arc).gauss(0.0, P.speed_sigma))
        if st.off_plan and arc[0] not in plan.designated:
            factor *= P.unprepared_factor
        tau = ae.time_min * factor
        pass_min = g.coster.pass_min(arc, unit.vehicle_lengths)
        h = headway_min(g)
        entry = earliest
        # following: time gap behind the last column that entered this segment in the same direction
        same = occ_same.get(arc, [])
        pred = max((o for o in same if o[0] <= entry + 1e-9), key=lambda o: o[0], default=None)
        if pred is not None and entry < pred[0] + pred[2] + h - 1e-6:
            conflicts += entry < pred[0] + pred[2] + h - TICK  # credits are rounded to the scheduler's tick
            entry = pred[0] + pred[2] + h
        # single-lane: wait for opposite columns to clear
        if not can_meet(H, arc[0], g.profile.width_m, g.profile.width_m):
            for _ in range(10):
                clash = [o for o in occ_opp.get(arc[0], []) if o[0] != arc[1] and o[1] < entry + tau + pass_min and entry < o[2]]
                if not clash:
                    break
                conflicts += entry < max(o[2] for o in clash) - TICK
                entry = max(o[2] for o in clash)
        exit_ = entry + tau
        block_exit = exit_
        # a lost element on this segment
        hit = [e for e in H.corridors[arc[0]].arc_elements(arc[1]) if e in closed_at]
        cor_hit = arc[0] in closed_corr
        turned = False
        if hit or cor_hit:
            L = max(H.corridors[arc[0]].length_m, 1.0)
            if hit:
                e = hit[0]
                x = H.corridors[arc[0]].element_chainage(e, arc[1]) / L
                t_lost, key = closed_at[e], e
            else:
                x, t_lost, key = 0.5, closed_corr[arc[0]], f"corridor:{arc[0]}"
            t_reach = entry + tau * x
            if t_lost <= t_reach:
                t_learn = know[uid].get(key, INF)
                t_turn = t_learn if entry < t_learn < t_reach else t_reach
                if t_turn >= t_reach:
                    st.reached += 1
                back = t_turn + (t_turn - entry) + P.turn_around_min
                st.known.add(key)
                st.off_plan = True
                st.path.append((arc, entry, back))
                heapq.heappush(heap, (back, next(tick), uid))
                turned = True
        if turned:
            continue
        # breakdowns on the segment
        for k, inc in breakdowns:
            if inc.unit == uid and k not in st.broken and entry <= inc.time < exit_:
                st.broken.add(k)
                exit_ += inc.duration
                if not can_pull_aside(H, arc, g.profile.width_m):
                    block_exit = exit_
        km = ae.length_m / 1000.0
        if not deterministic and P.breakdown_rate > 0:
            r = _rng(seed, rep, "breakdown", uid, arc, len(st.path))
            if r.random() < 1.0 - math.exp(-P.breakdown_rate * unit.n * km):
                stop = r.uniform(*P.repair_min)
                if not can_pull_aside(H, arc, g.profile.width_m):
                    exit_ += stop
                    block_exit = exit_
        if pred is not None:
            gap = pred[1] + pred[2] + h
            if exit_ < gap - 1e-6:
                conflicts += exit_ < gap - TICK
                exit_ = gap  # slows down behind the column ahead
                block_exit = max(block_exit, exit_)
        occ_same.setdefault(arc, []).append((entry, block_exit, pass_min, uid))
        occ_opp.setdefault(arc[0], []).append((arc[1], entry, exit_ + pass_min))
        # exposure on roads in threat areas
        edges = H.arc_edges(arc)
        for u_, v_ in edges:
            if threat_at(u_, v_, entry) >= P.threat_level:
                st.exposure += unit.n * (exit_ - entry) * G[u_][v_]["length_m"] / max(ae.length_m, 1.0)
        st.path.append((arc, entry, exit_))
        st.node = H.arc_end(arc)
        if not st.off_plan:
            st.i += 1
        heapq.heappush(heap, (exit_, next(tick), uid))

    units = {}
    for uid, st in states.items():
        u = ctx.units[uid]
        c = plan.credits.get(uid)
        planned = c.arrive if c is not None else u.earliest + plan.routes[uid].time_min
        units[uid] = UnitOutcome(uid, st.arrived, planned, u.deadline, st.exposure, st.reached, st.off_plan, st.path)
    return RepOutcome(rep, units, list(script), conflicts)


def monte_carlo(H: CorridorGraph, ctx: MissionContext, plan: SimPlan, scripts: Callable[[int], list[SimIncident]] | list[SimIncident],
                reps: int = 200, seed: int = 0, params: SimParams | None = None) -> SimResult:
    """Replications with common random numbers; ``scripts(rep)`` gives the incidents of each replication."""
    out = []
    for rep in range(reps):
        script = scripts(rep) if callable(scripts) else list(scripts)
        out.append(run_once(H, ctx, plan, script, rep, seed, params))
    weights = {uid: u.weight for uid, u in ctx.units.items()}
    return SimResult(plan.name, out, weights)


# ---------------------------------------------------------------------------
# Incident scripts (section 10.3)
# ---------------------------------------------------------------------------


def plan_elements(H: CorridorGraph, plans: list[SimPlan]) -> list[str]:
    """Critical elements (bridges, tunnels, ...) of the routes of the plans compared."""
    out = set()
    for p in plans:
        for r in list(p.routes.values()) + [a for alts in p.alternates.values() for a in alts]:
            out |= {e for e in r.elements if H.elements[e].kind != "articulation"}
    return sorted(out)


def script_uniform(elements: list[str], window: tuple[float, float], seed: int = 0) -> Callable[[int], list[SimIncident]]:
    """S-U: one loss, uniform over the given elements, at a uniform time in the window."""
    def f(rep: int) -> list[SimIncident]:
        if not elements:
            return []
        r = _rng(seed, rep, "script-U")
        return [SimIncident("closure", r.uniform(*window), element=r.choice(elements), label="S-U")]
    return f


def script_weighted(likelihood: dict[str, float], window: tuple[float, float], seed: int = 0) -> Callable[[int], list[SimIncident]]:
    """S-RT: one loss drawn from an independent likelihood ranking (never COA-Net's own red team)."""
    items = sorted(likelihood.items())

    def f(rep: int) -> list[SimIncident]:
        if not items:
            return []
        r = _rng(seed, rep, "script-RT")
        e = r.choices([e for e, _ in items], weights=[max(p, 1e-6) for _, p in items])[0]
        return [SimIncident("closure", r.uniform(*window), element=e, label="S-RT")]
    return f


def script_adversary(H: CorridorGraph, ctx: MissionContext, plan: SimPlan, elements: list[str], time: float,
                     params: SimParams | None = None) -> tuple[str | None, float]:
    """S-ADV: the element whose loss at ``time`` does the most damage to this plan (exhaustive, deterministic replay)."""
    best, worst = None, -1.0
    weights = {uid: u.weight for uid, u in ctx.units.items()}
    for e in elements:
        rep = run_once(H, ctx, plan, [SimIncident("closure", time, element=e)], 0, 0, params, deterministic=True)
        dmg = sum(weights[k] * (min(max(0.0, u.delay), 600.0)) for k, u in rep.units.items())
        if dmg > worst:
            best, worst = e, dmg
    return best, worst


__all__ = ["RepOutcome", "SimIncident", "SimParams", "SimPlan", "SimResult", "UnitOutcome", "monte_carlo", "plan_elements",
           "plan_from", "run_once", "script_adversary", "script_uniform", "script_weighted"]

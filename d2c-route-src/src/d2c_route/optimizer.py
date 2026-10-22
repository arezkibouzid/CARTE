"""C4: network optimiser.

Per convoy: hard rules filter edges, soft rules shape a doctrine-weighted cost

    c_v(e) = t_v(e) * (1 + sum_k w_k * phi_k(e) + p_unknown * n_unknown(e))

where ``t_v(e)`` is the convoy travel time on the edge. A penalty method
produces k diverse candidates; the cheapest is the main supply route (MSR)
and the alternates (ASR) are chosen under the overlap/detour network rules.
Convoys are then deconflicted in time by prioritised planning: higher
priority convoys reserve road slots first and later convoys shift their
departure, or switch to an alternate, to keep the doctrinal headway.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import networkx as nx

from .expr import eval_feature, eval_predicate
from .graph import resolve, speed_kmh, ukey
from .schemas import (
    ConstraintProgram,
    Convoy,
    ConvoyConstraints,
    ConvoyPlan,
    EdgeSlot,
    Halt,
    MissionSpec,
    Plan,
    Route,
)
from .vehicles import ConvoyProfile

VEHICLE_INTERVAL_M = 50.0  # distance between vehicles in the column


# ---------------------------------------------------------------------------
# Edge costing
# ---------------------------------------------------------------------------


@dataclass
class EdgeEval:
    feasible: bool
    time_min: float
    cost: float
    features: dict[str, float]  # rule_id -> phi in [0, 1] (weighted later)
    unknown: list[str]  # hard rules that could not be decided
    violated: list[str]  # hard rules evaluated False


class EdgeCoster:
    """Evaluates every edge once for one convoy and its constraint set."""

    def __init__(
        self,
        G: nx.DiGraph,
        cc: ConvoyConstraints,
        profile: ConvoyProfile,
        unknown_penalty: float = 0.5,
    ):
        self.G = G
        self.cc = cc
        self.profile = profile
        self.vehicle = profile.as_vehicle()
        self.unknown_penalty = unknown_penalty
        self.speed_cap = min(profile.max_speed_kmh, cc.temporal.get("max_speed_kmh", 1e9))
        self.speed_factor = cc.temporal.get("speed_factor", 1.0)
        self.evals: dict[tuple[Any, Any], EdgeEval] = {}
        for u, v, d in G.edges(data=True):
            self.evals[(u, v)] = self.evaluate(d)

    def edge_speed(self, d: dict) -> float:
        s = min(speed_kmh(d), self.speed_cap) * self.speed_factor
        return max(1.0, s * (1.0 - 0.5 * d.get("weather", 0.0)))

    def evaluate(self, d: dict) -> EdgeEval:
        violated, unknown = [], []
        for r in self.cc.hard:
            ok = eval_predicate(r.predicate, d, self.vehicle)
            if ok is False or (ok is None and r.on_unknown == "forbid"):
                violated.append(r.id)
            elif ok is None:
                unknown.append(r.id)
        t = d["length_m"] / 1000.0 / self.edge_speed(d) * 60.0
        feats = {}
        penalty = 0.0
        for term in self.cc.soft:
            phi = eval_feature(term.feature, d, self.vehicle)
            if phi > 0:
                feats[term.rule_id] = phi
                penalty += term.weight * phi
        penalty += self.unknown_penalty * len(unknown)
        return EdgeEval(not violated, t, t * (1.0 + penalty), feats, unknown, violated)

    def refresh(self, edges: list[tuple[Any, Any]]) -> None:
        """Re-evaluate edges after an incident edited their attributes."""
        for u, v in edges:
            if self.G.has_edge(u, v):
                self.evals[(u, v)] = self.evaluate(self.G[u][v])

    def weight_fn(self, multipliers: dict[tuple[Any, Any], float] | None = None):
        evals = self.evals
        mult = multipliers or {}

        def w(u, v, _d):
            e = evals[(u, v)]
            if not e.feasible:
                return None
            return e.cost * mult.get(ukey(u, v), 1.0)

        return w

    def route(self, nodes: list[Any]) -> Route:
        length = time = cost = 0.0
        feats: dict[str, float] = {}
        for u, v in zip(nodes[:-1], nodes[1:]):
            d = self.G[u][v]
            e = self.evals[(u, v)]
            length += d["length_m"]
            time += e.time_min
            cost += e.cost
            for k, phi in e.features.items():
                feats[k] = feats.get(k, 0.0) + phi * d["length_m"]
            for k in e.unknown:
                feats["unknown:" + k] = feats.get("unknown:" + k, 0.0) + d["length_m"]
        return Route(nodes=list(nodes), length_m=length, travel_time_min=time, cost=cost, features=feats)


# ---------------------------------------------------------------------------
# Candidate routes
# ---------------------------------------------------------------------------


def shortest(G: nx.DiGraph, s: Any, t: Any, weight) -> list[Any] | None:
    try:
        return nx.dijkstra_path(G, s, t, weight=weight)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None


def overlap(G: nx.DiGraph, a: Route, b: Route) -> float:
    """Share of route ``a``'s length that is also used by ``b``."""
    if a.length_m <= 0:
        return 0.0
    kb = {ukey(u, v) for u, v in b.edges()}
    shared = sum(G[u][v]["length_m"] for u, v in a.edges() if ukey(u, v) in kb)
    return shared / a.length_m


def diverse_routes(
    coster: EdgeCoster,
    s: Any,
    t: Any,
    k: int = 5,
    penalty: float = 2.0,
    base_multipliers: dict | None = None,
    sort: bool = True,
) -> list[Route]:
    """Penalty method: re-solve while inflating the cost of already used roads.

    With ``sort=False`` routes keep discovery order, so the first one is the
    optimum under ``base_multipliers`` (used for stability-aware repair)."""
    mult = dict(base_multipliers or {})
    routes: list[Route] = []
    seen: set[tuple] = set()
    for _ in range(2 * k):
        nodes = shortest(coster.G, s, t, coster.weight_fn(mult))
        if nodes is None:
            break
        if tuple(nodes) not in seen:
            seen.add(tuple(nodes))
            routes.append(coster.route(nodes))
            if len(routes) >= k:
                break
        for u, v in zip(nodes[:-1], nodes[1:]):
            key = ukey(u, v)
            mult[key] = mult.get(key, 1.0) * penalty
    if sort:
        routes.sort(key=lambda r: r.cost)
    return routes


def select_alternates(G: nx.DiGraph, msr: Route, candidates: list[Route], net: dict[str, float]) -> tuple[list[Route], list[str]]:
    n_alt = int(net.get("min_alternates", 0))
    max_ov = net.get("max_overlap", 1.0)
    max_detour = net.get("max_detour", float("inf"))
    chosen: list[Route] = []
    msgs = []
    for r in sorted(candidates, key=lambda r: r.cost):
        if r.nodes == msr.nodes or len(chosen) >= n_alt:
            continue
        if r.cost > max_detour * msr.cost:
            continue
        if overlap(G, r, msr) > max_ov or any(overlap(G, r, c) > max_ov for c in chosen):
            continue
        chosen.append(r)
    if len(chosen) < n_alt:
        msgs.append(
            f"only {len(chosen)} alternate(s) satisfy overlap <= {max_ov:.0%} and detour <= {max_detour}x "
            f"(required {n_alt})"
        )
    return chosen, msgs


# ---------------------------------------------------------------------------
# Scheduling and deconfliction
# ---------------------------------------------------------------------------


@dataclass
class Reservations:
    """Road occupancy intervals already granted to other convoys."""

    slots: dict[tuple[Any, Any], list[tuple[float, float, str]]] = field(default_factory=dict)

    def add(self, plan: ConvoyPlan) -> None:
        for s in plan.schedule:
            self.slots.setdefault(ukey(s.u, s.v), []).append((s.t_in, s.t_out, plan.convoy_id))

    def remove(self, convoy_id: str) -> None:
        for k in list(self.slots):
            self.slots[k] = [x for x in self.slots[k] if x[2] != convoy_id]

    def first_conflict(self, schedule: list[EdgeSlot], headway: float, me: str) -> tuple[EdgeSlot, float, str] | None:
        """Earliest conflicting slot and the time the road becomes free for us."""
        for s in schedule:
            for t_in, t_out, other in self.slots.get(ukey(s.u, s.v), ()):
                if other == me:
                    continue
                if s.t_in < t_out + headway and t_in < s.t_out + headway:
                    return s, t_out + headway, other
        return None

    @classmethod
    def from_plan(cls, plan: Plan, exclude: set[str] = frozenset()) -> "Reservations":
        r = cls()
        for cid, cp in plan.convoys.items():
            if cid not in exclude:
                r.add(cp)
        return r


def schedule_route(
    coster: EdgeCoster,
    route: Route,
    departure: float,
    temporal: dict[str, float],
) -> tuple[list[EdgeSlot], list[Halt], float]:
    halt_every = temporal.get("halt_every_min")
    halt_len = temporal.get("halt_duration_min", 0.0)
    column_m = coster.profile.column_length_m(VEHICLE_INTERVAL_M)
    t = departure
    driving = 0.0
    slots, halts = [], []
    for u, v in route.edges():
        if halt_every and driving >= halt_every:
            halts.append(Halt(node=u, t_start=t, duration_min=halt_len))
            t += halt_len
            driving = 0.0
        d = coster.G[u][v]
        dt = coster.evals[(u, v)].time_min
        pass_time = column_m / 1000.0 / coster.edge_speed(d) * 60.0  # tail of the column
        slots.append(EdgeSlot(u=u, v=v, t_in=t, t_out=t + dt + pass_time))
        t += dt
        driving += dt
    return slots, halts, t


def _deconflict(
    coster: EdgeCoster,
    route: Route,
    convoy_id: str,
    earliest: float,
    cc: ConvoyConstraints,
    reservations: Reservations,
    max_shifts: int = 200,
) -> tuple[float, list[EdgeSlot], list[Halt], float, list[str]]:
    headway = cc.network.get("min_headway_min", 0.0)
    dep = earliest
    behind: list[str] = []
    for _ in range(max_shifts):
        slots, halts, arr = schedule_route(coster, route, dep, cc.temporal)
        c = reservations.first_conflict(slots, headway, convoy_id)
        if c is None:
            msgs = []
            if dep > earliest + 1e-6:
                msgs.append(
                    f"departure delayed {dep - earliest:.0f} min to keep {headway:.0f} min headway behind {', '.join(behind)}"
                )
            return dep, slots, halts, arr, msgs
        slot, free_at, other = c
        if other not in behind:
            behind.append(other)
        dep += max(free_at - slot.t_in, 0.5)
    raise RuntimeError(f"could not deconflict {convoy_id}")


def plan_convoy(
    G: nx.DiGraph,
    convoy: Convoy,
    cc: ConvoyConstraints,
    profile: ConvoyProfile,
    reservations: Reservations,
    unknown_penalty: float = 0.5,
    start_node: Any = None,
    start_time: float | None = None,
    previous: Route | None = None,
    stability: float = 0.0,
    k: int = 5,
    coster: EdgeCoster | None = None,
) -> ConvoyPlan:
    """Plan one convoy. ``previous`` + ``stability`` in (0, 1) discount the roads of
    the current plan, so a repair only deviates where it has to."""
    coster = coster or EdgeCoster(G, cc, profile, unknown_penalty)
    s = start_node if start_node is not None else resolve(G, convoy.origin)
    t = resolve(G, convoy.destination)
    dep0 = start_time if start_time is not None else convoy.earliest_departure_min
    base = {}
    if previous is not None and stability > 0:
        base = {ukey(u, v): 1.0 - stability for u, v in previous.edges()}
    if s == t:
        r = Route(nodes=[s], length_m=0, travel_time_min=0, cost=0)
        return ConvoyPlan(convoy_id=convoy.id, msr=r, departure_min=dep0, arrival_min=dep0, status="delivered")
    cands = diverse_routes(coster, s, t, k=k, base_multipliers=base, sort=not base)
    if not cands:
        return ConvoyPlan(
            convoy_id=convoy.id,
            msr=None,
            departure_min=dep0,
            status="infeasible",
            messages=["no route satisfies the hard constraints"],
        )
    if base:
        # the first candidate is optimal under the stability bonus; alternates ranked on true cost
        cands = [cands[0]] + sorted(cands[1:], key=lambda r: r.cost)
    msr = cands[0]
    asrs, msgs = select_alternates(G, msr, cands[1:], cc.network)
    if len(asrs) < cc.network.get("min_alternates", 0):
        # second chance with a stronger diversity penalty
        more = diverse_routes(coster, s, t, k=k, penalty=6.0, base_multipliers=base)
        asrs, msgs = select_alternates(G, msr, [r for r in more if r.nodes != msr.nodes], cc.network)

    # deconflict: try MSR, then each ASR as the main route if MSR arrives late
    options = []
    for i, r in enumerate([msr] + asrs):
        dep, slots, halts, arr, dmsgs = _deconflict(coster, r, convoy.id, dep0, cc, reservations)
        late = convoy.latest_arrival_min is not None and arr > convoy.latest_arrival_min
        options.append((late, arr if late else i, i, r, dep, slots, halts, arr, dmsgs))
        if not late:
            break
    late, _, i, main, dep, slots, halts, arr, dmsgs = min(options, key=lambda o: (o[0], o[1]))
    routes = [msr] + asrs
    alts = [r for j, r in enumerate(routes) if j != i]
    if i > 0:
        msgs.append(f"alternate {i} promoted to MSR: it meets the time window, the original MSR does not")
    return ConvoyPlan(
        convoy_id=convoy.id,
        msr=main,
        asrs=alts,
        departure_min=dep,
        arrival_min=arr,
        schedule=slots,
        halts=halts,
        status="late" if late else "ok",
        messages=msgs + dmsgs,
    )


def plan_network(
    G: nx.DiGraph,
    mission: MissionSpec,
    program: ConstraintProgram,
    profiles: dict[str, ConvoyProfile],
    deconflict: bool = True,
    k: int = 5,
    costers: dict[str, EdgeCoster] | None = None,
) -> Plan:
    """Prioritised planning: convoys in (priority, departure) order reserve roads.

    Pass a dict as ``costers`` to keep each convoy's evaluated graph for fast
    incident repair."""
    order = sorted(mission.convoys, key=lambda c: (c.priority, c.earliest_departure_min, c.id))
    reservations = Reservations()
    plan = Plan(mission_id=mission.id, convoys={})
    for c in order:
        coster = EdgeCoster(G, program.convoys[c.id], profiles[c.id], program.unknown_penalty)
        if costers is not None:
            costers[c.id] = coster
        cp = plan_convoy(
            G,
            c,
            program.convoys[c.id],
            profiles[c.id],
            reservations if deconflict else Reservations(),
            program.unknown_penalty,
            k=k,
            coster=coster,
        )
        plan.convoys[c.id] = cp
        reservations.add(cp)
    plan.log.append(f"planned {len(order)} convoys in priority order: {[c.id for c in order]}")
    return plan


def shortest_path_baseline(G: nx.DiGraph, mission: MissionSpec, weight: str = "length_m") -> dict[str, list[Any] | None]:
    """Baseline 1: doctrine-blind shortest paths."""
    return {c.id: shortest(G, resolve(G, c.origin), resolve(G, c.destination), weight) for c in mission.convoys}


__all__ = [
    "EdgeCoster",
    "Reservations",
    "diverse_routes",
    "overlap",
    "plan_convoy",
    "plan_network",
    "schedule_route",
    "select_alternates",
    "shortest_path_baseline",
]

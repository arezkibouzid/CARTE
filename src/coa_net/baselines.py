"""Baselines B0-B3 of the experimental plan (methodology 10.4) on the same corridor graph and costers.

B0  fastest route per group under the physical requirements only (T0 road level); no alternate.
B1  vehicle-aware fastest route per group (T0 and T1 road and route requirements); no alternate.
B2  D2C-Route style: weighted-sum cost t x (1 + sum of w phi) with D2C-Route's soft-rule weights,
    alternates by the penalty method (cost x2 on used corridors, 5 routes, second chance x6), kept if
    the overlap with the MSR is at most 50 % and the cost at most 1.8 x the MSR's; one alternate.
B3  B2 plus the fix of methodology 8.3: the cheapest route (same cost) avoiding every bridge and
    tunnel of the MSR becomes ASR1 if it meets the 1.8 cost limit, whatever its overlap.

Every baseline returns a Design (recourse as in the greedy fallback), so that the same evaluation,
verification, movement credits and simulation apply to every pipeline.
"""

from __future__ import annotations

from typing import Callable

from .candidates import Route, allowed_halt_nodes, make_route, search
from .corridors import Arc, CorridorGraph
from .costing import ArcEval, GroupCoster
from .design import DEFAULT_ORDER, Design, DesignProblem, _recourse, evaluate
from .mission import Group
from .requirements import ActiveSet

# D2C-Route seed soft rules (feature weight), as in its rule base
D2C_WEIGHTS = {"built_up": 1.0, "hazmat_built_up": 2.0, "centrality": 0.5, "slope": 0.5, "road_class": 0.8, "threat": 3.0,
               "open_day": 0.3, "weather": 2.0, "unknown": 0.5}


def _route(H: CorridorGraph, g: Group, arcs, aps: set[str] | frozenset = frozenset()) -> Route:
    """``aps``: the group's articulation points, so that every pipeline is scored on the same critical elements."""
    return make_route(H, g, tuple(arcs), set(aps), allowed_halt_nodes(H, g))


def _path(H: CorridorGraph, coster: GroupCoster, g: Group, weight=None, banned_elements=None) -> tuple[Arc, ...] | None:
    pts = [g.origin, *g.via, g.destination]
    arcs: list[Arc] = []
    for a, b in zip(pts[:-1], pts[1:]):
        p = search(H, coster, a, b, weight, banned_elements)
        if p is None:
            return None
        arcs.extend(p)
    return tuple(arcs)


def make_design(prob: DesignProblem, name: str, msr: dict[str, Route], asrs: dict[str, list[Route]], order: list[str] | None = None,
                description: str = "") -> Design:
    H, crit = prob.H, prob.crit
    order = ["lateness"] + list(order or DEFAULT_ORDER)
    designated = {c for g, r in msr.items() for x in [r, *asrs.get(g, [])] for c in x.corridors}
    irreducible = {g: set(crit.irreducible.get(g, set())) for g in msr}
    accepted = {g: {c for c in r.elements if c not in irreducible[g] and all(c in a.elements for a in asrs.get(g, []))} for g, r in msr.items()}
    unplanned = {g: "no route" for g in prob.groups if g not in msr}
    d = Design(name, order, {}, msr, asrs, designated, accepted, irreducible, _recourse(crit, msr, asrs, order),
               solver=name, status="heuristic", unplanned=unplanned, description=description)
    d.objectives = evaluate(H, prob.groups, crit, d, prob.phase)
    return d


def b0(prob: DesignProblem) -> Design:
    """Fastest route under the physical (T0) requirements only."""
    msr = {}
    for gid, g in prob.groups.items():
        act = ActiveSet([q for q in g.active.reqs if q.tier == "T0"])
        c0 = GroupCoster(prob.H, g.profile, act, g.coster.phase, overrides=g.coster.overrides)
        arcs = _path(prob.H, c0, g)
        if arcs is not None:
            msr[gid] = _route(prob.H, g, arcs, set(prob.cands[gid].articulation))
    return make_design(prob, "B0", msr, {}, description="fastest route, physical requirements only")


def b1(prob: DesignProblem) -> Design:
    """Vehicle-aware fastest route (T0 and T1 requirements of the road and route levels)."""
    msr = {gid: cs.fastest for gid, cs in prob.cands.items() if cs.fastest is not None}
    return make_design(prob, "B1", msr, {}, description="vehicle-aware fastest route")


def d2c_arc_costs(H: CorridorGraph, g: Group) -> dict[Arc, float]:
    """D2C-Route's edge cost t x (1 + sum w phi), summed over the edges of each feasible arc."""
    co = g.coster
    heavy = "heavy" in g.profile.vehicle_classes
    medium_heavy = bool({"medium", "heavy"} & g.profile.vehicle_classes)
    day = co.phase == "day"
    out = {}
    for arc, ae in co.arcs.items():
        if not ae.ok:
            continue
        total = 0.0
        for u, v in H.arc_edges(arc):
            d = co.attrs(u, v)
            ev = co.evals.get((u, v)) or co.evaluate(u, v)
            pen = 0.0
            if d.get("built_up"):
                pen += D2C_WEIGHTS["built_up"] + (D2C_WEIGHTS["hazmat_built_up"] if g.hazmat else 0.0)
            pen += D2C_WEIGHTS["centrality"] * float(d.get("centrality", 0.0) or 0.0)
            if heavy and d.get("slope_pct") is not None:
                pen += D2C_WEIGHTS["slope"] * d["slope_pct"] / 15.0
            if medium_heavy:
                pen += D2C_WEIGHTS["road_class"] * d.get("road_rank", 5) / 6.0
            pen += D2C_WEIGHTS["threat"] * float(d.get("threat", 0.0) or 0.0)
            if day and not d.get("forest"):
                pen += D2C_WEIGHTS["open_day"]
            pen += D2C_WEIGHTS["weather"] * float(d.get("weather", 0.0) or 0.0)
            pen += D2C_WEIGHTS["unknown"] * len(ev.unknown)
            total += ev.time_min * (1.0 + pen)
        out[arc] = total
    return out


def _cost_fn(costs: dict[Arc, float], mult: dict[str, float]) -> Callable[[Arc, ArcEval], float]:
    return lambda arc, ae: costs.get(arc, ae.time_min) * mult.get(arc[0], 1.0)


def overlap(H: CorridorGraph, a: Route, b: Route) -> float:
    """Share of route a's length also used by b (D2C-Route's overlap)."""
    if a.length_m <= 0:
        return 0.0
    shared = sum(H.corridors[c].length_m for c in a.corridors & b.corridors)
    return shared / a.length_m


def _cost(costs: dict[Arc, float], r: Route) -> float:
    return sum(costs.get(a, 0.0) for a in r.arcs)


def diverse_routes(H: CorridorGraph, g: Group, costs: dict[Arc, float], k: int = 5, penalty: float = 2.0,
                   aps: set[str] | frozenset = frozenset()) -> list[Route]:
    mult: dict[str, float] = {}
    routes: list[Route] = []
    seen: set = set()
    for _ in range(2 * k):
        arcs = _path(H, g.coster, g, _cost_fn(costs, mult))
        if arcs is None:
            break
        if arcs not in seen:
            seen.add(arcs)
            routes.append(_route(H, g, arcs, aps))
            if len(routes) >= k:
                break
        for a in arcs:
            mult[a[0]] = mult.get(a[0], 1.0) * penalty
    routes.sort(key=lambda r: _cost(costs, r))
    return routes


def select_alternates(H: CorridorGraph, msr: Route, cands: list[Route], costs: dict[Arc, float], n_alt: int = 1,
                      max_overlap: float = 0.5, max_detour: float = 1.8) -> list[Route]:
    chosen: list[Route] = []
    base = _cost(costs, msr)
    for r in sorted(cands, key=lambda r: _cost(costs, r)):
        if r.key == msr.key or len(chosen) >= n_alt:
            continue
        if _cost(costs, r) > max_detour * base:
            continue
        if overlap(H, r, msr) > max_overlap or any(overlap(H, r, c) > max_overlap for c in chosen):
            continue
        chosen.append(r)
    return chosen


def b2(prob: DesignProblem, disjoint_fix: bool = False, n_alt: int = 1, max_overlap: float = 0.5, max_detour: float = 1.8) -> Design:
    H = prob.H
    msr: dict[str, Route] = {}
    asrs: dict[str, list[Route]] = {}
    for gid, g in prob.groups.items():
        costs = d2c_arc_costs(H, g)
        aps = set(prob.cands[gid].articulation)
        cands = diverse_routes(H, g, costs, aps=aps)
        if not cands:
            continue
        m = cands[0]
        alts = select_alternates(H, m, cands[1:], costs, n_alt, max_overlap, max_detour)
        if len(alts) < n_alt:
            more = diverse_routes(H, g, costs, penalty=6.0, aps=aps)
            alts = select_alternates(H, m, [r for r in more if r.key != m.key], costs, n_alt, max_overlap, max_detour)
        if disjoint_fix:
            crossings = frozenset(e for e in m.elements if H.elements[e].kind in ("bridge", "tunnel"))
            if crossings:
                arcs = _path(H, g.coster, g, _cost_fn(costs, {}), crossings)
                if arcs is not None:
                    r = _route(H, g, arcs, aps)
                    if _cost(costs, r) <= max_detour * _cost(costs, m):
                        alts = [r] + [a for a in alts if a.key != r.key]
        msr[gid] = m
        asrs[gid] = alts
    name = "B3" if disjoint_fix else "B2"
    desc = "D2C-Route style with disjoint alternates (8.3)" if disjoint_fix else "D2C-Route style (weighted sum, penalty alternates)"
    return make_design(prob, name, msr, asrs, description=desc)


def b3(prob: DesignProblem) -> Design:
    return b2(prob, disjoint_fix=True)


BASELINES: dict[str, Callable[[DesignProblem], Design]] = {"B0": b0, "B1": b1, "B2": b2, "B3": b3}

__all__ = ["BASELINES", "D2C_WEIGHTS", "b0", "b1", "b2", "b3", "d2c_arc_costs", "diverse_routes", "make_design", "overlap", "select_alternates"]

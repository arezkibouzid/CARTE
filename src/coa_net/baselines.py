"""Baselines B0-B1 of the experimental plan (methodology 10.4) on the same corridor graph and costers.

B0  fastest route per group under the physical requirements only (T0 road level); no alternate.
B1  vehicle-aware fastest route per group (T0 and T1 road and route requirements); no alternate.

Every baseline returns a Design (recourse as in the greedy fallback), so that the same evaluation,
verification, movement credits and simulation apply to every pipeline.
"""

from __future__ import annotations

from typing import Callable

from .candidates import Route, allowed_halt_nodes, make_route, search
from .corridors import Arc, CorridorGraph
from .costing import GroupCoster
from .design import DEFAULT_ORDER, Design, DesignProblem, _recourse, evaluate
from .mission import Group
from .requirements import ActiveSet

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


BASELINES: dict[str, Callable[[DesignProblem], Design]] = {"B0": b0, "B1": b1}

__all__ = ["BASELINES", "b0", "b1", "make_design"]

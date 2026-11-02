"""S3: candidate routes per convoy group (methodology 5, S3).

For each group, on the corridor graph with the group's feasibility mask:

1. supported Pareto routes: Dijkstra on a small grid of weightings of the
   criteria (time, threat, built-up, open ground, non-preferred roads); the
   weightings only generate candidates, they never decide;
2. route-level requirements: resource-constrained search by label setting
   (RTE.GAP_MAX driving time since the last halt area, RTE.CROSSINGS_MAX
   built-up crossings, RTE.RANGE distance since the last fuel point), with
   turn-expanded states for long vehicles (RTE.TURN);
3. replacement routes: for every critical element on a candidate, the best
   route that avoids it, plus the cheapest *pair* of routes sharing no
   critical element (min-cost flow of two units, element capacity 1);
4. articulation points of the feasible graph between origin and
   destination: irreducible single points of failure;
5. a pool of at most 30 routes that keeps the best route of every critical
   element signature.
"""

from __future__ import annotations

import heapq
import itertools
import math
import weakref
from dataclasses import dataclass, field
from typing import Any, Callable

import networkx as nx

from .corridors import Arc, CorridorGraph
from .costing import ArcEval, GroupCoster
from .graph import turn_angle
from .mission import Group

INF = float("inf")

# weightings (minutes per km-equivalent) used only to generate diverse supported routes
WEIGHT_GRID = [
    ("threat", (5.0, 0, 0, 0)),
    ("threat+", (20.0, 0, 0, 0)),
    ("towns", (0, 2.0, 0, 0)),
    ("towns+", (0, 8.0, 0, 0)),
    ("cover", (0, 0, 1.0, 0)),
    ("preferred", (0, 0, 0, 1.0)),
    ("preferred+", (0, 0, 0, 3.0)),
    ("balanced", (5.0, 2.0, 0.5, 1.0)),
    ("protected", (20.0, 8.0, 1.0, 3.0)),
]


def compact_names(names: list[str]) -> str:
    """Critical elements in driving order, runs of the same name counted: on BD TOPO a bridge is named after its road,
    so a route read "N166, N166, N166, ..." (eleven bridges); it now reads "N166 x11"."""
    out: list[str] = []
    for n in names:
        if out and out[-1][0] == n:
            out[-1][1] += 1
        else:
            out.append([n, 1])
    return ", ".join(n if k == 1 else f"{n} x{k}" for n, k in out)


@dataclass
class Route:
    group: str
    arcs: tuple[Arc, ...]
    nodes: list[Any]
    length_m: float
    time_min: float
    exposure: float  # km-equivalent
    builtup_m: float
    crossings: int  # built-up areas crossed
    open_m: float
    pref_m: float
    unknown_m: float
    max_threat: float
    elements: frozenset  # C(P), critical elements crossed
    corridors: frozenset  # A(P), undirected corridor ids
    halts: list[Any] | None  # halt areas used (None: no halt plan meets RTE.GAP_MAX)
    max_turn: float
    max_leg_km: float  # longest distance between fuel points
    tags: set[str] = field(default_factory=set)
    violations: list[tuple[str, str, str]] = field(default_factory=list)  # route-level (req id, tier, detail)

    @property
    def key(self) -> tuple[Arc, ...]:
        return self.arcs

    def dps(self, H: CorridorGraph) -> list[Any]:
        return [H.arc_start(a) for a in self.arcs] + ([H.arc_end(self.arcs[-1])] if self.arcs else [])

    def describe(self, H: CorridorGraph) -> str:
        els = [H.elements[e].name for e in self.ordered_elements(H) if H.elements[e].kind != "articulation"]
        via = f" via {compact_names(els)}" if els else ""
        return f"{self.length_m / 1000:.1f} km, {self.time_min:.1f} min{via}"

    def ordered_elements(self, H: CorridorGraph) -> list[str]:
        out: list[str] = []
        for a in self.arcs:
            for e in H.corridors[a[0]].arc_elements(a[1]):
                if e not in out:
                    out.append(e)
            n = H.arc_end(a)
            if f"AP-{n}" in self.elements and f"AP-{n}" not in out:
                out.append(f"AP-{n}")
        return out


@dataclass
class CandidateSet:
    group: str
    routes: list[Route]
    fastest: Route | None
    articulation: list[str]  # AP element ids (irreducible by definition)
    elements: set[str]  # union of C(P) over the pool
    replacement: dict[str, Route | None]  # best route avoiding each element (any length)
    notes: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    @property
    def best_time(self) -> float:
        return self.fastest.time_min if self.fastest else INF


# ---------------------------------------------------------------------------
# Search primitives
# ---------------------------------------------------------------------------


def _banned_sets(H: CorridorGraph, banned_elements) -> tuple[set[str], set[Any]]:
    corridors, nodes = set(), set()
    for e in banned_elements or ():
        el = H.elements[e]
        if el.kind == "articulation":
            nodes |= set(el.nodes)
        else:
            corridors |= H.element_corridors(e)
    return corridors, nodes


def search(
    H: CorridorGraph,
    coster: GroupCoster,
    src: Any,
    dst: Any,
    weight: Callable[[Arc, ArcEval], float] | None = None,
    banned_elements: frozenset | set | None = None,
    banned_arcs: set | None = None,
    max_turn: float | None = None,
) -> tuple[Arc, ...] | None:
    """Least-weight arc sequence from src to dst on the group's feasible corridor graph."""
    if src == dst:
        return ()
    bc, bn = _banned_sets(H, banned_elements)
    bad_arcs = banned_arcs or set()
    w = weight or (lambda arc, ae: ae.time_min)
    arcs = coster.arcs
    G = H.G
    tick = itertools.count()
    if max_turn is None:
        dist = {src: 0.0}
        prev: dict[Any, tuple[Any, Arc]] = {}
        heap = [(0.0, next(tick), src)]
        while heap:
            d, _, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > dist.get(u, INF):
                continue
            for arc, v in H.out.get(u, ()):
                if arc[0] in bc or arc in bad_arcs or (v in bn and v != dst):
                    continue
                ae = arcs.get(arc)
                if ae is None or not ae.ok:
                    continue
                nd = d + w(arc, ae)
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    prev[v] = (u, arc)
                    heapq.heappush(heap, (nd, next(tick), v))
        if dst not in prev:
            return None
        out = []
        n = dst
        while n != src:
            u, arc = prev[n]
            out.append(arc)
            n = u
        return tuple(reversed(out))
    # turn-expanded states (node, incoming arc)
    start = (src, None)
    dist2 = {start: 0.0}
    prev2: dict[tuple, tuple] = {}
    heap2 = [(0.0, next(tick), start)]
    goal = None
    while heap2:
        d, _, state = heapq.heappop(heap2)
        u, last = state
        if u == dst:
            goal = state
            break
        if d > dist2.get(state, INF):
            continue
        for arc, v in H.out.get(u, ()):
            if arc[0] in bc or arc in bad_arcs or (v in bn and v != dst):
                continue
            ae = arcs.get(arc)
            if ae is None or not ae.ok:
                continue
            if last is not None:
                if last[0] == arc[0]:
                    continue  # no U-turn on the same corridor
                a_nodes, b_nodes = H.arc_nodes(last), H.arc_nodes(arc)
                if turn_angle(G, a_nodes[-2], u, b_nodes[1]) > max_turn:
                    continue
            ns = (v, arc)
            nd = d + w(arc, ae)
            if nd < dist2.get(ns, INF):
                dist2[ns] = nd
                prev2[ns] = state
                heapq.heappush(heap2, (nd, next(tick), ns))
    if goal is None:
        return None
    out = []
    s = goal
    while s != start:
        out.append(s[1])
        s = prev2[s]
    return tuple(reversed(out))


def _arc_builtup_profile(H: CorridorGraph, coster: GroupCoster, arc: Arc) -> tuple[bool, bool, int]:
    flags = [coster.evals[e].builtup for e in H.arc_edges(arc)]
    runs = sum(1 for i, f in enumerate(flags) if f and (i == 0 or not flags[i - 1]))
    return (flags[0] if flags else False, flags[-1] if flags else False, runs)


def rcsp(
    H: CorridorGraph,
    coster: GroupCoster,
    src: Any,
    dst: Any,
    max_drive: float | None = None,
    halt_nodes: set | None = None,
    halt_min: float = 0.0,
    max_crossings: int | None = None,
    max_range_m: float | None = None,
    fuel_nodes: set | None = None,
    banned_elements: frozenset | set | None = None,
    max_turn: float | None = None,
    labels_per_node: int = 40,
) -> tuple[Arc, ...] | None:
    """Resource-constrained shortest path by label setting with dominance."""
    bc, bn = _banned_sets(H, banned_elements)
    halt_nodes = halt_nodes or set()
    fuel_nodes = fuel_nodes or set()
    G = H.G
    tick = itertools.count()
    # label: (cost, tick, node, last_arc, drive, crossings, dist, in_town, parent)
    root = (0.0, next(tick), src, None, 0.0, 0, 0.0, False, None)
    heap = [root]
    kept: dict[Any, list[tuple]] = {}

    def dominated(lab) -> bool:
        for o in kept.get(lab[2], ()):
            if (o[0] <= lab[0] + 1e-9 and o[4] <= lab[4] + 1e-9 and o[5] <= lab[5] and o[6] <= lab[6] + 1e-9
                    and o[7] == lab[7] and (max_turn is None or o[3] == lab[3])):
                return True
        return False

    while heap:
        lab = heapq.heappop(heap)
        cost, _, u, last, drive, cross, dist, in_town, _p = lab
        if u == dst:
            out = []
            x = lab
            while x[8] is not None:
                out.append(x[3])
                x = x[8]
            return tuple(reversed(out))
        if dominated(lab):
            continue
        bucket = kept.setdefault(u, [])
        if len(bucket) >= labels_per_node:
            continue
        bucket.append(lab)
        for arc, v in H.out.get(u, ()):
            if arc[0] in bc or (v in bn and v != dst):
                continue
            ae = coster.arcs.get(arc)
            if ae is None or not ae.ok:
                continue
            if max_turn is not None and last is not None:
                if last[0] == arc[0]:
                    continue
                if turn_angle(G, H.arc_nodes(last)[-2], u, H.arc_nodes(arc)[1]) > max_turn:
                    continue
            nd = drive + ae.time_min
            if max_drive is not None and nd > max_drive + 1e-9:
                continue
            first, lastb, runs = _arc_builtup_profile(H, coster, arc)
            nc = cross + runs - (1 if first and in_town and runs else 0)
            if max_crossings is not None and nc > max_crossings:
                continue
            ndist = dist + ae.length_m
            if max_range_m is not None and ndist > max_range_m:
                continue
            ncost = cost + ae.time_min
            if v in fuel_nodes:
                ndist = 0.0
            heapq.heappush(heap, (ncost, next(tick), v, arc, nd, nc, ndist, lastb, lab))
            if v in halt_nodes and max_drive is not None and v != dst:
                heapq.heappush(heap, (ncost + halt_min, next(tick), v, arc, 0.0, nc, ndist, lastb, lab))
    return None


# ---------------------------------------------------------------------------
# Route metrics and route-level checks
# ---------------------------------------------------------------------------


# halt areas far enough from critical elements, per corridor graph and (kinds, distance, halt areas): on a real
# map with thousands of halt candidates the check costs seconds and every candidate search asks for it
_FAR_FROM: "weakref.WeakKeyDictionary[CorridorGraph, dict]" = weakref.WeakKeyDictionary()


def _far_from(H: CorridorGraph, nodes: frozenset, kinds: frozenset, dmin: float) -> frozenset:
    """Nodes farther than ``dmin`` metres from every element of ``kinds`` (same distance as ``point_distance``)."""
    import numpy as np

    from .graph import is_geographic, node_xy

    G = H.G
    E = np.array([e.xy for e in H.elements.values() if e.kind in kinds], dtype=float).reshape(-1, 2)
    if not len(E) or not nodes:
        return frozenset(nodes)
    ns = sorted(nodes, key=str)
    P = np.array([node_xy(G, n) for n in ns], dtype=float)
    keep = np.ones(len(ns), bool)
    for i in range(0, len(ns), 512):
        a = P[i:i + 512]
        if is_geographic(G):
            lon1, lat1 = np.radians(a[:, :1]), np.radians(a[:, 1:])
            lon2, lat2 = np.radians(E[:, 0])[None, :], np.radians(E[:, 1])[None, :]
            h = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
            dist = 2 * 6_371_000 * np.arcsin(np.sqrt(h))
        else:
            dist = np.hypot(a[:, :1] - E[None, :, 0], a[:, 1:] - E[None, :, 1])
        keep[i:i + 512] = (dist > dmin).all(axis=1)
    return frozenset(n for n, k in zip(ns, keep) if k)


def allowed_halt_nodes(H: CorridorGraph, group: Group) -> set[Any]:
    """Halt areas, minus those too close to a critical element (PRO.CRITPOINT)."""
    G = H.G
    nodes = frozenset(n for n, d in G.nodes(data=True) if d.get("halt_capacity"))
    out = set(nodes)
    for r in group.active.of("PRO.CRITPOINT", ("T1",)):
        key = (frozenset(r.p("element_kinds")), float(r.p("no_stop_within_m")), nodes)
        memo = _FAR_FROM.setdefault(H, {})
        if key not in memo:
            memo[key] = _far_from(H, nodes, key[0], key[1])
        out &= memo[key]
    return out


def halt_plan(H: CorridorGraph, coster: GroupCoster, arcs: tuple[Arc, ...], max_drive: float | None, halt_nodes: set,
              driven_min: float = 0.0) -> list[Any] | None:
    """Fewest halts so that no stretch of driving exceeds max_drive (greedy farthest reach).

    ``driven_min``: driving already done since the last halt by a unit that continues from the first arc's start
    (re-planning on the move); that start may then be a halt itself if it is a halt node."""
    if max_drive is None or not arcs:
        return []
    times = [coster.arcs[a].time_min for a in arcs]
    ends = [H.arc_end(a) for a in arcs]
    total = sum(times)
    if driven_min + total <= max_drive + 1e-9:
        return []
    stops: list[Any] = []
    t_last = -driven_min
    cum = 0.0
    start = H.arc_start(arcs[0])
    best = (0.0, start) if driven_min > 0 and start in halt_nodes else None
    for i, (dt, n) in enumerate(zip(times, ends)):
        if dt > max_drive + 1e-9:
            return None
        if cum + dt - t_last > max_drive + 1e-9:
            if best is None:
                return None
            stops.append(best[1])
            t_last = best[0]
            best = None
            if cum + dt - t_last > max_drive + 1e-9:
                return None
        cum += dt
        if n in halt_nodes and i < len(arcs) - 1:
            best = (cum, n)
    return stops


def make_route(H: CorridorGraph, group: Group, arcs: tuple[Arc, ...], aps: set[str], halt_nodes: set, tags: set[str] | None = None) -> Route:
    coster = group.coster
    G = H.G
    aes = [coster.arcs[a] for a in arcs]
    nodes = H.path_nodes(arcs) if arcs else [group.origin]
    flags = [coster.evals[e].builtup for a in arcs for e in H.arc_edges(a)]
    crossings = sum(1 for i, f in enumerate(flags) if f and (i == 0 or not flags[i - 1]))
    elements = set()
    for a in arcs:
        elements |= set(H.corridors[a[0]].elements)
    interior = {H.arc_end(a) for a in arcs[:-1]}
    elements |= {e for e in aps if set(H.elements[e].nodes) & interior}
    max_turn = 0.0
    for a, b in zip(arcs[:-1], arcs[1:]):
        an, bn = H.arc_nodes(a), H.arc_nodes(b)
        max_turn = max(max_turn, turn_angle(G, an[-2], an[-1], bn[1]))
    leg = 0.0
    max_leg = 0.0
    for a, ae in zip(arcs, aes):
        leg += ae.length_m
        max_leg = max(max_leg, leg)
        if G.nodes[H.arc_end(a)].get("fuel_point"):
            leg = 0.0
    act = group.active
    max_drive = act.param("RTE.GAP_MAX", "max_drive_min", None)
    r = Route(
        group=group.id,
        arcs=tuple(arcs),
        nodes=nodes,
        length_m=sum(a.length_m for a in aes),
        time_min=sum(a.time_min for a in aes),
        exposure=sum(a.exposure for a in aes),
        builtup_m=sum(a.builtup_m for a in aes),
        crossings=crossings,
        open_m=sum(a.open_m for a in aes),
        pref_m=sum(a.pref_m for a in aes),
        unknown_m=sum(a.unknown_m for a in aes),
        max_threat=max((a.max_threat for a in aes), default=0.0),
        elements=frozenset(elements),
        corridors=frozenset(a[0] for a in arcs),
        halts=halt_plan(H, coster, tuple(arcs), max_drive, halt_nodes),
        max_turn=max_turn,
        max_leg_km=max_leg / 1000.0,
        tags=set(tags or ()),
    )
    r.violations = route_violations(r, group)
    return r


def route_violations(r: Route, group: Group) -> list[tuple[str, str, str]]:
    """Route-level (RTE) requirement violations of one route."""
    act = group.active
    out = []
    for q in act.of("RTE.GAP_MAX"):
        if r.halts is None:
            out.append((q.id, q.tier, f"no halt plan keeps driving under {q.p('max_drive_min')} min between halt areas"))
    for q in act.of("RTE.TURN"):
        if group.profile.length_m >= q.p("min_vehicle_length_m") and r.max_turn > q.p("max_turn_deg") + 1e-6:
            out.append((q.id, q.tier, f"turn of {r.max_turn:.0f} deg > {q.p('max_turn_deg')} deg"))
    for q in act.of("RTE.CROSSINGS_MAX"):
        if q.p("element") == "built_up" and r.crossings > q.p("max_count"):
            out.append((q.id, q.tier, f"{r.crossings} built-up crossings > {q.p('max_count')}"))
    for q in act.of("RTE.RANGE"):
        if r.max_leg_km > q.p("max_km"):
            out.append((q.id, q.tier, f"{r.max_leg_km:.0f} km between fuel points > {q.p('max_km')} km"))
    return out


def hard_violations(r: Route) -> list[tuple[str, str, str]]:
    return [v for v in r.violations if v[1] in ("T0", "T1")]


# ---------------------------------------------------------------------------
# Articulation points and disjoint pairs
# ---------------------------------------------------------------------------


def _reachable(H: CorridorGraph, coster: GroupCoster, src: Any, dst: Any, removed: Any) -> bool:
    seen = {src}
    stack = [src]
    while stack:
        u = stack.pop()
        if u == dst:
            return True
        for arc, v in H.out.get(u, ()):
            if v == removed or v in seen:
                continue
            ae = coster.arcs.get(arc)
            if ae is not None and ae.ok:
                seen.add(v)
                stack.append(v)
    return False


def articulation_points(H: CorridorGraph, group: Group, fastest: tuple[Arc, ...]) -> list[str]:
    """Decision points every feasible route of the group must pass (irreducible SPOFs)."""
    out = []
    for a in fastest[:-1]:
        n = H.arc_end(a)
        if n in (group.origin, group.destination):
            continue
        if not _reachable(H, group.coster, group.origin, group.destination, n):
            out.append(H.add_articulation(n).id)
    return out


def _flow_network(H: CorridorGraph, coster) -> dict[tuple[Any, Any], tuple[int, int]]:
    """(tail, head) -> (capacity, cost) of the element-capacity network used by :func:`disjoint_pair`.

    Each usable arc is u -> ("a", arc) [arc time, capacity 2] -> its critical
    elements in order, each split into in -> out with capacity 1 -> v.
    Element nodes are shared by every arc that crosses the element."""
    net: dict[tuple[Any, Any], tuple[int, int]] = {}
    for u, outs in H.out.items():
        for arc, v in outs:
            ae = coster.arcs.get(arc)
            if ae is None or not ae.ok:
                continue
            head = ("a", arc)
            net[(u, head)] = (2, int(round(ae.time_min * 100)))
            prev = head
            for e in H.corridors[arc[0]].arc_elements(arc[1]):
                ein, eout = ("e", e, "in"), ("e", e, "out")
                net[(prev, ein)] = (2, 0)
                net[(ein, eout)] = (1, 0)
                prev = eout
            net[(prev, v)] = (2, 0)
    return net


def _two_unit_min_cost_flow(net: dict[tuple[Any, Any], tuple[int, int]], s: Any, t: Any) -> dict[tuple[Any, Any], int] | None:
    """Minimum-cost flow of value 2 from s to t by successive shortest paths.

    Two Dijkstra runs on the residual network (the second with the first run's
    distances as potentials, so reduced costs stay non-negative). Exact for
    non-negative costs, and much faster than a general network simplex on
    large corridor graphs. Returns the flow on each edge, or None if two units
    cannot be routed."""
    tails, heads, caps, costs = [], [], [], []
    adj: dict[Any, list[int]] = {}
    for (a, b), (c, w) in net.items():
        i = len(heads)
        tails += [a, b]
        heads += [b, a]
        caps += [c, 0]
        costs += [w, -w]
        adj.setdefault(a, []).append(i)
        adj.setdefault(b, []).append(i + 1)
    if s not in adj or t not in adj:
        return None
    pot: dict[Any, float] = {}
    for _ in range(2):
        dist: dict[Any, float] = {s: 0.0}
        pred: dict[Any, int] = {}
        tie = itertools.count()
        pq = [(0.0, next(tie), s)]
        while pq:
            d, _, a = heapq.heappop(pq)
            if d > dist[a]:
                continue
            pa = pot.get(a, 0.0)
            for i in adj[a]:
                if caps[i] <= 0:
                    continue
                b = heads[i]
                nd = d + costs[i] + pa - pot.get(b, 0.0)
                if nd < dist.get(b, math.inf):
                    dist[b] = nd
                    pred[b] = i
                    heapq.heappush(pq, (nd, next(tie), b))
        if t not in dist:
            return None
        node = t
        while node != s:
            i = pred[node]
            caps[i] -= 1
            caps[i ^ 1] += 1
            node = tails[i]
        pot = {n: pot.get(n, 0.0) + d for n, d in dist.items()}
    return {(tails[i], heads[i]): caps[i + 1] for i in range(0, len(heads), 2) if caps[i + 1] > 0}


def disjoint_pair(H: CorridorGraph, group: Group, max_turn: float | None = None) -> tuple[tuple[Arc, ...], tuple[Arc, ...]] | None:
    """Cheapest pair of routes that share no critical element (min-cost flow, element capacity 1).

    Corridors without critical elements have capacity 2, so the two routes may
    share ordinary roads; only bridges, tunnels and the like must be distinct."""
    flow_e = _two_unit_min_cost_flow(_flow_network(H, group.coster), group.origin, group.destination)
    if flow_e is None:
        return None
    flow: dict[Any, dict[Any, int]] = {}
    for (a, b), f in flow_e.items():
        flow.setdefault(a, {})[b] = f
    paths = []
    for _ in range(2):
        node = group.origin
        arcs: list[Arc] = []
        guard = 0
        while node != group.destination and guard < 100000:
            guard += 1
            nxt = next((m for m, f in flow.get(node, {}).items() if f > 0), None)
            if nxt is None:
                return None
            flow[node][nxt] -= 1
            if isinstance(nxt, tuple) and nxt and nxt[0] == "a":
                arcs.append(nxt[1])
            node = nxt
        paths.append(_remove_loops(H, arcs, group.origin))
    return paths[0], paths[1]


def _remove_loops(H: CorridorGraph, arcs: list[Arc], src: Any) -> tuple[Arc, ...]:
    """Drop cycles from an arc sequence (a flow decomposition may contain some)."""
    out: list[Arc] = []
    first = {src: 0}
    for a in arcs:
        out.append(a)
        n = H.arc_end(a)
        if n in first:
            out = out[: first[n]]
            first = {k: v for k, v in first.items() if v <= len(out)}
        else:
            first[n] = len(out)
    return tuple(out)


# ---------------------------------------------------------------------------
# Candidate pool
# ---------------------------------------------------------------------------


def _weight_fn(H: CorridorGraph, coster: GroupCoster, wx: float, wb: float, wo: float, wp: float):
    def w(arc: Arc, ae: ArcEval) -> float:
        return ae.time_min + wx * ae.exposure + wb * ae.builtup_m / 1000 + wo * ae.open_m / 1000 + wp * ae.pref_m / 1000

    return w


def generate_candidates(
    H: CorridorGraph,
    group: Group,
    max_routes: int = 30,
    keep_ratio: float = 3.0,
    replacement_cap: int = 60,
    origin: Any = None,
    seed_routes: list[tuple[Arc, ...]] | None = None,
) -> CandidateSet:
    """Candidate pool of one group (from ``origin`` when re-planning from a current position).

    ``seed_routes`` (repair) are arc sequences that must stay in the pool when still feasible, typically
    the remaining parts of the routes designated before the incident, so that stability can keep them."""
    coster = group.coster
    act = group.active
    src = group.origin if origin is None else origin
    dst = group.destination
    max_turn = None
    for q in act.of("RTE.TURN", ("T0", "T1")):
        if group.profile.length_m >= q.p("min_vehicle_length_m"):
            max_turn = q.p("max_turn_deg") if max_turn is None else min(max_turn, q.p("max_turn_deg"))
    halt_nodes = allowed_halt_nodes(H, group)
    max_drive = act.param("RTE.GAP_MAX", "max_drive_min", None)
    halt_min = act.param("SCH.HALT", "halt_min", 15.0)
    max_cross = None
    for q in act.of("RTE.CROSSINGS_MAX"):
        if q.p("element") == "built_up":
            max_cross = q.p("max_count") if max_cross is None else min(max_cross, q.p("max_count"))
    max_range = act.param("RTE.RANGE", "max_km", None)
    fuel_nodes = {n for n, d in H.G.nodes(data=True) if d.get("fuel_point")}
    waypoints = [src, *group.via, dst]
    g_view = group if origin is None else _GroupAt(group, src)

    def run(banned=None, weight=None, constrained=False) -> tuple[Arc, ...] | None:
        legs: list[Arc] = []
        for a, b in zip(waypoints[:-1], waypoints[1:]):
            if constrained:
                p = rcsp(H, coster, a, b, max_drive, halt_nodes, halt_min, max_cross, max_range * 1000 if max_range else None,
                         fuel_nodes, banned, max_turn)
            else:
                p = search(H, coster, a, b, weight, banned, None, max_turn)
            if p is None:
                return None
            legs.extend(p)
        return tuple(legs)

    def build(arcs, tag) -> Route:
        return make_route(H, g_view, arcs, aps, halt_nodes, {tag})

    pool: dict[tuple, Route] = {}
    notes: list[str] = []
    aps: set[str] = set()
    fast = run()
    if fast is None:
        return CandidateSet(group.id, [], None, [], set(), {}, notes=["no route satisfies the T0/T1 road requirements"])
    aps = set(articulation_points(H, g_view, fast)) if not group.via else set()
    fastest = build(fast, "fastest")
    if hard_violations(fastest) and (max_drive or max_cross is not None or max_range):
        alt = run(constrained=True)
        if alt is not None:
            fastest = build(alt, "fastest:constrained")
    pool[fastest.key] = fastest

    def add(arcs, tag, banned=None) -> Route | None:
        if arcs is None:
            return None
        if arcs in pool:
            pool[arcs].tags.add(tag)
            return pool[arcs]
        r = build(arcs, tag)
        if hard_violations(r):
            alt = run(banned=banned, constrained=True)
            if alt is None:
                return None
            r = build(alt, tag + ":constrained") if alt not in pool else pool[alt]
            if hard_violations(r):
                return None
        if r.time_min > keep_ratio * max(fastest.time_min, 1e-6):
            return r  # returned to the caller (fallback information) but not pooled
        pool.setdefault(r.key, r)
        return pool[r.key]

    seeded: list[tuple] = []
    for arcs in seed_routes or []:
        arcs = tuple(arcs)
        if arcs and H.arc_start(arcs[0]) == src and H.arc_end(arcs[-1]) == dst and all(coster.arc_ok(a) for a in arcs):
            r = add(arcs, "previous")
            if r is not None and r.key in pool:
                seeded.append(r.key)
    for name, (wx, wb, wo, wp) in WEIGHT_GRID:
        add(run(weight=_weight_fn(H, coster, wx, wb, wo, wp)), f"pareto:{name}")
    if max_cross is not None:
        add(run(constrained=True), "rcsp:crossings")

    replacement: dict[str, Route | None] = {}
    queue = [e for r in list(pool.values()) for e in sorted(r.elements, key=str) if e not in aps]
    while queue and len(replacement) < replacement_cap:
        e = queue.pop(0)
        if e in replacement or e in aps:
            continue
        r = add(run(banned={e}), f"avoid:{e}", {e})
        replacement[e] = r
        if r is not None:
            queue.extend(x for x in sorted(r.elements, key=str) if x not in replacement and x not in aps)
    # penalty method (cost multiplied on used corridors) for a few more distinct routes
    used: dict[str, float] = {}
    for _ in range(3):
        for r in pool.values():
            for c in r.corridors:
                used[c] = used.get(c, 1.0) * 1.6
        add(run(weight=lambda arc, ae: ae.time_min * used.get(arc[0], 1.0)), "diverse")
    for base in sorted(pool.values(), key=lambda r: r.time_min)[:3]:
        crit = frozenset(base.elements - aps)
        if crit:
            add(run(banned=crit), "disjoint-of-best", crit)
    if not group.via:
        pair = disjoint_pair(H, g_view, max_turn)
        if pair:
            for p in pair:
                if p:
                    add(p, "disjoint-pair")

    routes = sorted(pool.values(), key=lambda r: (r.time_min, r.length_m))
    keep: list[Route] = [pool[k] for k in dict.fromkeys(seeded)]
    seen_sig = {r.elements for r in keep}
    for r in routes:  # best route of each critical-element signature first
        if r.elements not in seen_sig:
            seen_sig.add(r.elements)
            keep.append(r)
    for r in routes:
        if r not in keep:
            keep.append(r)
    if len(keep) > max(max_routes, len(seeded)):
        notes.append(f"pool capped at {max_routes} of {len(keep)} candidates")
        keep = sorted(keep[: max(max_routes, len(seeded))], key=lambda r: (r.time_min, r.length_m))
    else:
        keep = sorted(keep, key=lambda r: (r.time_min, r.length_m))
    elements = set().union(*(r.elements for r in keep)) if keep else set()
    for e in elements:
        if e not in replacement and e not in aps:
            replacement[e] = add(run(banned={e}), f"avoid:{e}", {e})
    return CandidateSet(
        group=group.id,
        routes=keep,
        fastest=fastest,
        articulation=sorted(aps),
        elements=elements,
        replacement=replacement,
        notes=notes,
        stats={"pool": len(keep), "generated": len(pool)},
    )


class _GroupAt:
    """A group seen from another origin (repair from a current position)."""

    def __init__(self, group: Group, origin: Any):
        self._g = group
        self.origin = origin

    def __getattr__(self, name):
        return getattr(self._g, name)


__all__ = [
    "CandidateSet",
    "Route",
    "allowed_halt_nodes",
    "articulation_points",
    "compact_names",
    "disjoint_pair",
    "generate_candidates",
    "halt_plan",
    "make_route",
    "rcsp",
    "route_violations",
    "search",
]

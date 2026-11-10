"""Exercise mode: the organisers' exercise format on the real Scenario data.

The preparation exercise asks for *k* itineraries between two drawn zones
that never intersect and stay at least *d* km apart, avoid towns above a
population threshold, and come with a layer of difficult-manoeuvre areas, a
GeoJSON with the itineraries and the location and curvature radius of the
difficult turns, and a text describing the decision process.

COA-Net handles it with its own machinery:

* the exercise text is compiled into requirement instances (two templates
  added for it: ``SEG.TOWN`` and ``NET.DISTINCT``) next to the seed doctrine;
* every road is evaluated by the group coster (S2-S3 masks: load, gauge,
  surface, slope, closures, towns), unknowns priced as risk;
* ``NET.DISTINCT`` is a network-level design problem solved by seeded
  sequential exclusion: diverse via-node candidates seed the first
  itinerary, each next itinerary is the fastest route outside a *d*-km
  exclusion corridor around the previous ones; the best feasible set wins
  (lexicographic: all k found, then total time);
* a checker re-verifies every constraint from the geometry;
* turns, difficult areas and the decision log are computed facts.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import numpy as np

from .graph import ukey
from .realdata import L93, radius_profile
from .requirements import Requirement, Source


# ---------------------------------------------------------------------------
# exercise text -> spec and requirement instances
# ---------------------------------------------------------------------------

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "un": 1, "deux": 2, "trois": 3, "quatre": 4}


@dataclass
class ExerciseSpec:
    k: int = 3
    min_sep_m: float = 5000.0
    no_intersect: bool = True
    min_town_pop: int | None = 10000
    vehicle: str = "pfm_carrier"
    n_vehicles: int = 6
    origin_zone: str = "South"
    destination_zone: str = "North"
    turn_radius_warn_m: float = 30.0  # assumption: difficult for a ~12 m carrier
    turn_radius_crit_m: float = 15.0  # assumption: needs a manoeuvre or a guide
    steep_slope_pct: float = 8.0
    turn_penalty_min: float = 1.0  # minutes per 90-degree-equivalent junction turn (quadratic in the deflection)
    sharp_turn_deg: float = 75.0  # junction deflection above which the turn reads as critical (radius < 15 m over 20 m)
    sharp_turn_min: float = 1.0  # extra minutes for such a turn
    outputs: list[str] = field(default_factory=list)
    recognised: list[tuple[str, str]] = field(default_factory=list)  # (context element, quote)
    assumptions: list[str] = field(default_factory=list)


def parse_exercise(text: str) -> tuple[ExerciseSpec, list[Requirement]]:
    """Pattern compiler for the exercise statement (the language-model path can replace it; same schema)."""
    s = " ".join(text.split())
    low = s.lower()
    spec = ExerciseSpec(outputs=[])
    reqs: list[Requirement] = []

    stops = [x.end() for x in re.finditer(r"[.;:!?](?=\s|$)", s)]

    def quote(m: re.Match) -> str:
        """The clause of the statement that holds the match (between punctuation stops)."""
        a, b = m.span()
        start = max((x for x in stops if x <= a), default=0)
        end = min((x for x in stops if x >= b), default=len(s))
        return s[start:end].strip(" -")

    m = re.search(r"(\d+|one|two|three|four|five)\s+(?:different\s+)?itinerar", low)
    if m:
        spec.k = int(m.group(1)) if m.group(1).isdigit() else WORDS[m.group(1)]
        spec.recognised.append(("number of itineraries", quote(m)))
    m = re.search(r"(?:at least|minimum(?: of)?)\s*(\d+(?:[.,]\d+)?)\s*km\s*(?:apart|away|from each other)", low)
    if m:
        spec.min_sep_m = float(m.group(1).replace(",", ".")) * 1000
        spec.recognised.append(("separation between itineraries", quote(m)))
    m = re.search(r"not intersect", low)
    spec.no_intersect = bool(m)
    if m:
        spec.recognised.append(("itineraries must not intersect", quote(m)))
    if spec.k > 1:
        reqs.append(Requirement(id="X-01", template="NET.DISTINCT", tier="T1",
                                params={"k": spec.k, "min_sep_km": spec.min_sep_m / 1000, "no_intersect": spec.no_intersect},
                                source=Source(document="exercise", quote=s[:200]), trigger="must",
                                explanation=f"{spec.k} itineraries, pairwise at least {spec.min_sep_m / 1000:g} km apart"))
    m = re.search(r"towns? of more than\s*(\d+(?:[.,]\d+)?)\s*(k|000|\s*000)?\s*inhabitants", low)
    if m:
        n = float(m.group(1).replace(",", "."))
        spec.min_town_pop = int(n * 1000) if (m.group(2) or "").strip() in ("k", "000") else int(n)
        spec.recognised.append(("towns to avoid (population threshold)", quote(m)))
        reqs.append(Requirement(id="X-02", template="SEG.TOWN", tier="T1", params={"min_population": spec.min_town_pop},
                                on_unknown="risk", source=Source(document="exercise", quote=quote(m)), trigger="avoid",
                                explanation=f"no road inside a town of more than {spec.min_town_pop} inhabitants"))
    else:
        spec.min_town_pop = None
    m = re.search(r"(floatable|floating) bridge|pont flottant", low)
    if m:
        spec.vehicle = "pfm_carrier"
        spec.recognised.append(("convoy: motorised floating bridge (PFM)", quote(m)))
        spec.assumptions.append(f"PFM column assumed to be {spec.n_vehicles} carriers of the `pfm_carrier` type "
                                "(dimensions and weight in vehicles.py are assumptions to confirm with the ETLO)")
    m = re.search(r"from the (\w+) zone to the (\w+) zone", low)
    if m:
        spec.origin_zone, spec.destination_zone = m.group(1).capitalize(), m.group(2).capitalize()
        spec.recognised.append(("origin and destination zones", quote(m)))
    for key, pat in (("itinerary layer", r"layer[^.;]*itinerar"), ("difficult-manoeuvre layer", r"layer[^.;]*maneuver|layer[^.;]*manoeuvr"),
                     ("GeoJSON with turns and curvature radius", r"geojson[^.;]*(curvature|radius)"),
                     ("decision-process text", r"text file[^.;]*decision")):
        m = re.search(pat, low)
        if m:
            spec.outputs.append(key)
            spec.recognised.append(("output: " + key, quote(m)))
    spec.assumptions += [
        f"difficult turn: centreline radius below {spec.turn_radius_warn_m:g} m, critical below {spec.turn_radius_crit_m:g} m "
        "(path length / heading change over a 20 m window of the BD TOPO centreline)",
        f"steep: average section slope above {spec.steep_slope_pct:g} % (BD TOPO Z_INI/Z_FIN, central value; with the stated "
        "altitude precision a 50 m section is only known to within about 4 points of slope)",
        "itinerary separation measured between road centrelines in Lambert-93 (EPSG:2154)",
        f"junction turns priced in the route search: {spec.turn_penalty_min:g} min x (deflection/90)^2, "
        f"+{spec.sharp_turn_min:g} min above {spec.sharp_turn_deg:g} degrees (column slowing and manoeuvring; to calibrate)",
    ]
    return spec, reqs


# ---------------------------------------------------------------------------
# road arrays: one row per directed edge, geometry in Lambert-93
# ---------------------------------------------------------------------------

@dataclass
class RoadArrays:
    G: nx.DiGraph
    edges: list[tuple[Any, Any]]
    node_index: dict[Any, int]
    nodes: list[Any]
    u: np.ndarray
    v: np.ndarray
    time_min: np.ndarray
    length_m: np.ndarray
    ok: np.ndarray
    lines: np.ndarray  # shapely LineStrings, EPSG:2154
    tree: Any
    node_xy: np.ndarray  # EPSG:2154

    @classmethod
    def build(cls, G: nx.DiGraph, evals: dict, lines: np.ndarray | None = None) -> "RoadArrays":
        import shapely
        from pyproj import Transformer

        edges = list(G.edges)
        nodes = list(G.nodes)
        idx = {n: i for i, n in enumerate(nodes)}
        tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
        if lines is None:
            lines = edge_lines(G, edges)
        nx_, ny_ = tr.transform(np.array([G.nodes[n]["x"] for n in nodes]), np.array([G.nodes[n]["y"] for n in nodes]))
        return cls(
            G=G, edges=edges, node_index=idx, nodes=nodes,
            u=np.array([idx[a] for a, _ in edges]), v=np.array([idx[b] for _, b in edges]),
            time_min=np.array([evals[e].time_min for e in edges]), length_m=np.array([G[a][b]["length_m"] for a, b in edges]),
            ok=np.array([evals[e].ok for e in edges]), lines=lines, tree=shapely.STRtree(lines),
            node_xy=np.column_stack([nx_, ny_]),
        )

    def near(self, geom, dist_m: float) -> np.ndarray:
        """Edge indices whose geometry comes within ``dist_m`` of ``geom`` (strictly closer is excluded)."""
        return self.tree.query(geom, predicate="dwithin", distance=dist_m)


def edge_lines(G: nx.DiGraph, edges: list | None = None) -> np.ndarray:
    """Lambert-93 LineStrings of the edges (in ``G.edges`` order unless given)."""
    import shapely
    from pyproj import Transformer

    edges = list(G.edges) if edges is None else edges
    tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    counts = np.fromiter((len(G[a][b]["coords"]) for a, b in edges), int, len(edges))
    flat = np.empty((counts.sum(), 2))
    k = 0
    for (a, b), c in zip(edges, counts):
        flat[k:k + c] = G[a][b]["coords"]
        k += c
    x, y = tr.transform(flat[:, 0], flat[:, 1])
    return shapely.linestrings(np.column_stack([x, y]), indices=np.repeat(np.arange(len(edges)), counts))


class Router:
    """Multi-source multi-target shortest paths on the masked road arrays (SciPy Dijkstra on junctions). Paths are edge lists."""

    def __init__(self, R: RoadArrays, sources: np.ndarray, targets: np.ndarray, penalty: np.ndarray | None = None):
        self.R = R
        self.N = len(R.nodes)
        self.s, self.t = self.N, self.N + 1
        self.sources, self.targets = sources, targets
        self.base_cost = R.time_min * (penalty if penalty is not None else 1.0)
        self.eidx = _edge_index(R)

    def _matrix(self, mask: np.ndarray):
        from scipy.sparse import csr_matrix

        R = self.R
        e = np.flatnonzero(mask)
        rows = np.concatenate([R.u[e], np.full(len(self.sources), self.s), self.targets])
        cols = np.concatenate([R.v[e], self.sources, np.full(len(self.targets), self.t)])
        w = np.concatenate([np.maximum(self.base_cost[e], 1e-6), np.full(len(self.sources), 1e-6), np.full(len(self.targets), 1e-6)])
        return csr_matrix((w, (rows, cols)), shape=(self.N + 2, self.N + 2))

    def _edges(self, nodes: list[int] | None) -> list[int] | None:
        if nodes is None or len(nodes) < 2:
            return None
        return [self.eidx[(a, b)] for a, b in zip(nodes[:-1], nodes[1:])]

    def shortest(self, mask: np.ndarray) -> list[int] | None:
        from scipy.sparse.csgraph import dijkstra

        d, pred = dijkstra(self._matrix(mask), indices=self.s, return_predecessors=True)
        if not np.isfinite(d[self.t]):
            return None
        return self._edges(_pred_path(pred, self.s, self.t, self.N))

    def via_candidates(self, mask: np.ndarray, cell_m: float = 3000.0, max_ratio: float = 1.6, n: int = 30,
                       jaccard_max: float = 0.6) -> list[list[int]]:
        """Diverse s-t paths through via junctions on a lattice, cheapest first (plateau / via-node alternatives)."""
        from scipy.sparse.csgraph import dijkstra

        A = self._matrix(mask)
        dS, pS = dijkstra(A, indices=self.s, return_predecessors=True)
        dT, pT = dijkstra(A.T.tocsr(), indices=self.t, return_predecessors=True)
        tot = dS[: self.N] + dT[: self.N]
        return _pick_vias(tot, self.R.node_xy, cell_m, max_ratio, n, jaccard_max,
                          lambda w: self._edges(_join(_pred_path(pS, self.s, w, self.N), _pred_path(pT, self.t, w, self.N))), self.R)


def edge_headings(R: RoadArrays, probe_m: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    """Heading (degrees, Lambert-93) of every edge where it leaves its start node and where it enters its end node."""
    import shapely

    L = shapely.length(R.lines)
    d = np.minimum(probe_m, L / 2)
    p0 = shapely.get_coordinates(shapely.get_point(R.lines, 0))
    p1 = shapely.get_coordinates(shapely.line_interpolate_point(R.lines, d))
    q1 = shapely.get_coordinates(shapely.line_interpolate_point(R.lines, L - d))
    q0 = shapely.get_coordinates(shapely.get_point(R.lines, -1))
    h_start = np.degrees(np.arctan2(p1[:, 1] - p0[:, 1], p1[:, 0] - p0[:, 0]))
    h_end = np.degrees(np.arctan2(q0[:, 1] - q1[:, 1], q0[:, 0] - q1[:, 0]))
    return h_start, h_end


class TurnRouter:
    """Turn-aware shortest paths: Dijkstra on the line graph (one state per directed road, one arc per allowed turn).

    A turn of deflection a costs ``turn_min * (a / 90)**2`` minutes (column
    slowing down and manoeuvring at junctions) plus ``sharp_min`` when the
    junction is sharper than ``sharp_deg``; U-turns are not allowed; turns
    sharper than ``max_turn_deg`` (RTE.TURN) are removed. Paths are lists of
    edge indices.
    """

    def __init__(self, R: RoadArrays, sources: np.ndarray, targets: np.ndarray, penalty: np.ndarray | None = None,
                 turn_min: float = 1.0, sharp_deg: float = 75.0, sharp_min: float = 1.0, max_turn_deg: float | None = None):
        import pandas as pd

        self.R = R
        E = len(R.edges)
        self.E, self.s, self.t = E, E, E + 1
        self.base_cost = R.time_min * (penalty if penalty is not None else 1.0)
        src = np.zeros(len(R.nodes), bool)
        src[sources] = True
        dst = np.zeros(len(R.nodes), bool)
        dst[targets] = True
        self.src_edges = np.flatnonzero(src[R.u])
        self.dst_edges = np.flatnonzero(dst[R.v])
        h_start, h_end = edge_headings(R)
        a = pd.DataFrame({"e1": np.arange(E), "n": R.v})
        b = pd.DataFrame({"e2": np.arange(E), "n": R.u})
        m = a.merge(b, on="n")
        e1, e2 = m["e1"].to_numpy(), m["e2"].to_numpy()
        keep = R.v[e2] != R.u[e1]  # no U-turn onto the same road
        e1, e2 = e1[keep], e2[keep]
        defl = np.abs((h_start[e2] - h_end[e1] + 180.0) % 360.0 - 180.0)
        if max_turn_deg is not None:
            ok = defl <= max_turn_deg
            e1, e2, defl = e1[ok], e2[ok], defl[ok]
        self.e1, self.e2, self.defl = e1, e2, defl
        self.turn_cost = turn_min * (defl / 90.0) ** 2 + np.where(defl > sharp_deg, sharp_min, 0.0)
        mid = shapely_midpoints(R.lines)
        self.mid = mid

    def _matrix(self, mask: np.ndarray):
        from scipy.sparse import csr_matrix

        ok = mask[self.e1] & mask[self.e2]
        se = self.src_edges[mask[self.src_edges]]
        de = self.dst_edges[mask[self.dst_edges]]
        rows = np.concatenate([self.e1[ok], np.full(len(se), self.s), de])
        cols = np.concatenate([self.e2[ok], se, np.full(len(de), self.t)])
        w = np.concatenate([np.maximum(self.base_cost[self.e2[ok]] + self.turn_cost[ok], 1e-6),
                            np.maximum(self.base_cost[se], 1e-6), np.full(len(de), 1e-6)])
        return csr_matrix((w, (rows, cols)), shape=(self.E + 2, self.E + 2))

    def shortest(self, mask: np.ndarray) -> list[int] | None:
        from scipy.sparse.csgraph import dijkstra

        d, pred = dijkstra(self._matrix(mask), indices=self.s, return_predecessors=True)
        if not np.isfinite(d[self.t]):
            return None
        return _pred_path(pred, self.s, self.t, self.E)

    def via_candidates(self, mask: np.ndarray, cell_m: float = 3000.0, max_ratio: float = 1.6, n: int = 30,
                       jaccard_max: float = 0.6) -> list[list[int]]:
        from scipy.sparse.csgraph import dijkstra

        A = self._matrix(mask)
        dS, pS = dijkstra(A, indices=self.s, return_predecessors=True)
        dT, pT = dijkstra(A.T.tocsr(), indices=self.t, return_predecessors=True)
        tot = dS[: self.E] + dT[: self.E]
        return _pick_vias(tot, self.mid, cell_m, max_ratio, n, jaccard_max,
                          lambda w: _join(_pred_path(pS, self.s, w, self.E), _pred_path(pT, self.t, w, self.E)), self.R)

    def turn_cost_of(self, edges: list[int]) -> np.ndarray:
        self._index()
        return np.array([self._cost.get((a, b), np.nan) for a, b in zip(edges[:-1], edges[1:])])

    def _index(self) -> None:
        if not hasattr(self, "_key"):
            self._key = {(int(a), int(b)): float(d) for a, b, d in zip(self.e1, self.e2, self.defl)}
            self._cost = {(int(a), int(b)): float(c) for a, b, c in zip(self.e1, self.e2, self.turn_cost)}

    def turns_of(self, edges: list[int]) -> np.ndarray:
        """Deflection (degrees) at each junction of an edge path."""
        self._index()
        return np.array([self._key.get((a, b), np.nan) for a, b in zip(edges[:-1], edges[1:])])


def shapely_midpoints(lines: np.ndarray) -> np.ndarray:
    import shapely

    return shapely.get_coordinates(shapely.line_interpolate_point(lines, 0.5, normalized=True))


def _pred_path(pred: np.ndarray, src: int, dst: int, n_real: int) -> list[int] | None:
    out = [dst]
    while out[-1] != src:
        p = pred[out[-1]]
        if p < 0:
            return None
        out.append(int(p))
    out.reverse()
    return [i for i in out if i < n_real]


def _join(a: list[int] | None, b: list[int] | None) -> list[int] | None:
    """s..w (forward) + t..w (on the transposed graph) -> s..w..t."""
    if a is None or b is None:
        return None
    return a + list(reversed(b))[1:]


def _pick_vias(tot, xy, cell_m, max_ratio, n, jaccard_max, build, R) -> list[list[int]]:
    best = np.min(tot)
    if not np.isfinite(best):
        return []
    good = np.flatnonzero(tot <= best * max_ratio)
    cells: dict[tuple[int, int], int] = {}
    for i in good[np.argsort(tot[good])]:
        cells.setdefault((int(xy[i, 0] // cell_m), int(xy[i, 1] // cell_m)), int(i))
    out, sigs = [], []
    for w in sorted(cells.values(), key=lambda i: tot[i]):
        edges = build(w)
        if not edges:
            continue
        nodes = [int(R.u[edges[0]])] + [int(R.v[e]) for e in edges]
        if len(set(nodes)) != len(nodes):
            continue
        pts = R.node_xy[nodes]
        sig = {(int(x // 1000), int(y // 1000)) for x, y in pts}
        if any(len(sig & o) / len(sig | o) > jaccard_max for o in sigs):
            continue
        sigs.append(sig)
        out.append(edges)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------
# solver
# ---------------------------------------------------------------------------

@dataclass
class Itinerary:
    id: str
    nodes: list[Any]
    edge_idx: list[int]
    time_min: float
    length_m: float
    line: Any  # shapely LineString EPSG:2154
    facts: dict = field(default_factory=dict)
    turns: list[dict] = field(default_factory=list)


@dataclass
class DistinctResult:
    itineraries: list[Itinerary]
    tried: int
    feasible_sets: int
    seconds: float
    log: list[str]
    alternatives: list[tuple[float, list[float]]]  # (total time, per-itinerary times) of the other feasible sets


def _edge_index(R: RoadArrays) -> dict[tuple[int, int], int]:
    return {(int(a), int(b)): i for i, (a, b) in enumerate(zip(R.u, R.v))}


def route_line(R: RoadArrays, edges: list[int]):
    """The route as one LineString in travel order (edge polylines are stored in their travel direction)."""
    import shapely

    parts = [shapely.get_coordinates(R.lines[e]) for e in edges]
    pts = np.concatenate([parts[0]] + [p[1:] for p in parts[1:]]) if parts else np.empty((0, 2))
    return shapely.linestrings(pts)


def solve_distinct(R: RoadArrays, sources: np.ndarray, targets: np.ndarray, k: int, sep_m: float,
                   n_seeds: int = 20, n_second: int = 3, penalty: np.ndarray | None = None,
                   time_budget_s: float = 300.0, router=None) -> DistinctResult:
    """k itineraries pairwise >= sep_m apart: seeded sequential exclusion, best total time."""
    import shapely

    t0 = time.perf_counter()
    router = router or Router(R, sources, targets, penalty)
    log: list[str] = []
    base = R.ok.copy()

    def to_edges(path: list[int]) -> list[int]:
        return path

    def line_of(edges: list[int]):
        return route_line(R, edges)

    def exclude(mask: np.ndarray, edges: list[int]) -> np.ndarray:
        m = mask.copy()
        geom = shapely.union_all(R.lines[edges])
        m[R.near(geom, sep_m)] = False
        return m

    def cost(edges: list[int]) -> float:
        c = float(router.base_cost[edges].sum())
        if isinstance(router, TurnRouter):
            c += float(np.nansum(router.turn_cost_of(edges)))
        return c

    seeds = router.via_candidates(base, n=n_seeds)
    log.append(f"{len(seeds)} diverse first-itinerary seeds (via-point alternatives within 1.6x the fastest)")
    sets: list[tuple[float, list[list[int]]]] = []
    tried = 0
    for si, seed in enumerate(seeds):
        if time.perf_counter() - t0 > time_budget_s:
            log.append(f"time budget reached after {si} seeds")
            break
        e1 = to_edges(seed)
        mask1 = exclude(base, e1)
        if k == 1:
            sets.append((cost(e1), [e1]))
            continue
        seconds = router.via_candidates(mask1, n=n_second) or []
        for p2 in seconds:
            tried += 1
            e2 = to_edges(p2)
            chosen = [e1, e2]
            mask = exclude(mask1, e2)
            ok = True
            for _ in range(k - 2):
                p = router.shortest(mask)
                if p is None:
                    ok = False
                    break
                e = to_edges(p)
                chosen.append(e)
                mask = exclude(mask, e)
            if ok:
                sets.append((sum(cost(e) for e in chosen), chosen))
    uniq: dict[tuple, tuple[float, list[list[int]]]] = {}
    for c, es in sets:
        uniq.setdefault(tuple(sorted(tuple(e) for e in es)), (c, es))
    sets = sorted(uniq.values(), key=lambda t: t[0])
    log.append(f"{tried} completions tried, {len(sets)} distinct feasible sets of {k}")
    its: list[Itinerary] = []
    if sets:
        best = sets[0][1]
        # name by west-to-east order of the midpoints
        lines = [line_of(e) for e in best]
        order = sorted(range(len(best)), key=lambda i: shapely.line_interpolate_point(lines[i], 0.5, normalized=True).x)
        for j, i in enumerate(order):
            e = best[i]
            nodes = [R.nodes[R.u[e[0]]]] + [R.nodes[R.v[x]] for x in e]
            its.append(Itinerary(id=f"IT-{j + 1}", nodes=nodes, edge_idx=e, time_min=float(R.time_min[e].sum()),
                                 length_m=float(R.length_m[e].sum()), line=lines[i]))
    alts = [(t, [float(R.time_min[e].sum()) for e in es]) for t, es in sets[1:6]]
    return DistinctResult(its, tried, len(sets), time.perf_counter() - t0, log, alts)


# ---------------------------------------------------------------------------
# facts per itinerary, turns, difficult areas
# ---------------------------------------------------------------------------

def resample(line, step_m: float = 5.0) -> np.ndarray:
    import shapely

    n = max(2, int(line.length // step_m) + 1)
    d = np.linspace(0, line.length, n)
    return shapely.get_coordinates(shapely.line_interpolate_point(line, d))


def chord_radius(p: np.ndarray, half: int) -> np.ndarray:
    """Radius of curvature at each resampled point: path length of the window / heading change over it.

    ``p`` is resampled at a constant step; the window spans ``half`` steps on
    each side. On a circular arc longer than the window this returns the arc
    radius exactly; on a corner of angle a it returns window / a (a 90 degree
    corner over a 20 m window reads 12.7 m), so corners and junction turns are
    graded on the same scale. Ends and straight stretches read inf.
    """
    r = np.full(len(p), math.inf)
    if len(p) <= 2 * half + 1:
        return r
    seg = np.diff(p, axis=0)
    step = np.linalg.norm(seg, axis=1)
    th = np.unwrap(np.arctan2(seg[:, 1], seg[:, 0]))
    # heading change between the segment entering the window and the one leaving it
    d = np.abs(th[2 * half - 1:] - th[: len(th) - 2 * half + 1])
    # path length between the midpoints of those two segments
    L = (np.convolve(step, np.ones(2 * half), mode="valid") - 0.5 * step[: len(d)] - 0.5 * step[2 * half - 1:])
    with np.errstate(divide="ignore", invalid="ignore"):
        rr = L / d
    rr[~np.isfinite(rr)] = math.inf
    r[half:half + len(rr)] = rr
    return r


def turns_on(line, warn_m: float, crit_m: float, step_m: float = 5.0, half_chord_m: float = 10.0) -> list[dict]:
    """Difficult turns along a route: local minima of the chord radius below ``warn_m``."""
    from pyproj import Transformer

    p = resample(line, step_m)
    half = max(1, int(round(half_chord_m / step_m)))
    r = chord_radius(p, half)
    below = r < warn_m
    out = []
    tr = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    i = 0
    while i < len(p):
        if not below[i]:
            i += 1
            continue
        j = i
        while j < len(p) and below[j]:
            j += 1
        m = i + int(np.argmin(r[i:j]))
        a, c = max(0, m - 2 * half), min(len(p) - 1, m + 2 * half)
        h1 = math.degrees(math.atan2(*(p[m] - p[a])[::-1]))
        h2 = math.degrees(math.atan2(*(p[c] - p[m])[::-1]))
        defl = abs((h2 - h1 + 180) % 360 - 180)
        lon, lat = tr.transform(p[m, 0], p[m, 1])
        out.append({"lon": round(float(lon), 6), "lat": round(float(lat), 6), "x": float(p[m, 0]), "y": float(p[m, 1]),
                    "radius_m": round(float(r[m]), 1), "deflection_deg": round(defl, 0),
                    "severity": "critical" if r[m] < crit_m else "difficult", "km": round(m * step_m / 1000, 2)})
        i = j
    return out


def itinerary_facts(R: RoadArrays, it: Itinerary, evals: dict, towns_small, spec: ExerciseSpec, need_width_m: float) -> dict:
    G = R.G
    names: list[str] = []
    bridges, tunnels, fords, unknown_w, narrow, steep, builtup_m = [], 0, 0, 0.0, [], 0.0, 0.0
    uncertain_w = 0.0  # roads whose rounded width may be under the column's need
    max_slope = 0.0
    slope_known = False
    sym_m: dict[str, float] = {}
    risks: dict[str, int] = {}
    for e in it.edge_idx:
        a, b = R.edges[e]
        d = G[a][b]
        nm = (d.get("name") or "").split(";")[0].replace(" ", "")  # OSM writes "D 766" (and "N 24;E 50")
        if re.match(r"^[ADNCRE]\d", nm) and (not names or names[-1] != nm):
            names.append(nm)
        if d.get("bridge"):
            bridges.append((nm, R.lines[e]))
        tunnels += bool(d.get("tunnel"))
        fords += bool(d.get("ford"))
        if d.get("width_m") is None:
            unknown_w += d["length_m"]
        elif d["width_m"] < need_width_m:
            narrow.append(d["length_m"])
        if d.get("slope_pct") is not None:
            slope_known = True
            max_slope = max(max_slope, d["slope_pct"])
            if d["slope_pct"] > spec.steep_slope_pct:
                steep += d["length_m"]
        if d.get("built_up"):
            builtup_m += d["length_m"]
        if d.get("scan_symbol"):
            sym_m[d["scan_symbol"]] = sym_m.get(d["scan_symbol"], 0.0) + d["length_m"]
        for rid, detail in evals[R.edges[e]].unknown:
            risks[rid] = risks.get(rid, 0) + 1
            if "may be under" in detail:
                uncertain_w += d["length_m"]
    crossed = []
    if towns_small is not None and len(towns_small):
        hit = towns_small[towns_small.intersects(it.line)]
        crossed = sorted(set(hit["town"])) if "town" in hit else []
    return {
        "length_km": round(it.length_m / 1000, 1), "time_min": round(it.time_min, 1),
        "roads": names[:40], "bridges": len(bridges), "tunnels": tunnels, "fords": fords,
        "width_unknown_km": round(unknown_w / 1000, 1), "narrow_km": round(sum(narrow) / 1000, 1),
        "width_uncertain_km": round(uncertain_w / 1000, 1),
        "steep_km": round(steep / 1000, 2), "max_slope_pct": max_slope, "builtup_km": round(builtup_m / 1000, 1),
        "built_up_towns_crossed": crossed, "unknown_risks": risks,
        "slope_known": slope_known,
        "map_symbols_km": {k: round(v / 1000, 1) for k, v in sorted(sym_m.items(), key=lambda kv: -kv[1])},
    }


def difficult_areas(R: RoadArrays, corridor, spec: ExerciseSpec, towns_big, builtup, need_width_m: float,
                    single_lane_m: float, buffer_m: float = 60.0, extra: list[tuple[str, Any]] | None = None):
    """Polygons (EPSG:4326) where a PFM column manoeuvres with difficulty, inside the study corridor."""
    import geopandas as gpd
    import shapely

    G = R.G
    idx = R.tree.query(corridor, predicate="intersects")
    seen = set()
    rows = []
    for e in idx:
        a, b = R.edges[e]
        k = ukey(a, b)
        if k in seen:
            continue
        seen.add(k)
        d = G[a][b]
        if d.get("road_rank", 5) > 5:
            continue
        line = R.lines[e]
        kinds = []
        if d.get("bridge"):
            kinds.append(("bridge (load class unknown)" if d.get("mlc") is None else "bridge", 1))
        if d.get("tunnel"):
            kinds.append(("tunnel (clearance unknown)" if d.get("max_height_m") is None else "tunnel", 1))
        if d.get("ford"):
            kinds.append(("ford", 1))
        if d.get("width_m") is not None and d["width_m"] < single_lane_m:
            kinds.append((f"carriageway < {single_lane_m:g} m", 1))
        if d.get("slope_pct") is not None and d["slope_pct"] > spec.steep_slope_pct:
            kinds.append((f"slope > {spec.steep_slope_pct:g} %", 1))
        if d.get("road_rank", 5) <= 4:  # sharp bends only on the roads a PFM column would use
            p = shapely.get_coordinates(line)
            if len(p) >= 3 and line.length > 20:
                q = resample(line, 5.0)
                r = chord_radius(q, 2)
                if (r < spec.turn_radius_crit_m).any():
                    for i in np.flatnonzero(r < spec.turn_radius_crit_m)[::4]:
                        rows.append({"kind": f"bend radius < {spec.turn_radius_crit_m:g} m", "geometry": shapely.Point(q[i]).buffer(buffer_m)})
        for kind, _ in kinds:
            rows.append({"kind": kind, "geometry": line.buffer(buffer_m)})
    for kind, geom in extra or []:
        rows.append({"kind": kind, "geometry": geom.buffer(buffer_m)})
    gdf = gpd.GeoDataFrame(rows, geometry="geometry", crs=L93) if rows else gpd.GeoDataFrame(columns=["kind", "geometry"], geometry="geometry", crs=L93)
    if builtup is not None and len(builtup):
        bu = builtup.to_crs(L93)
        bu = bu[bu.intersects(corridor)]
        gdf = gpd.GeoDataFrame(
            list(gdf.to_dict("records")) + [{"kind": "built-up area (narrow streets, traffic)", "geometry": g.intersection(corridor)} for g in bu.geometry],
            geometry="geometry", crs=L93)
    if towns_big is not None and len(towns_big):
        tb = towns_big.to_crs(L93)
        gdf = gpd.GeoDataFrame(
            list(gdf.to_dict("records")) + [{"kind": f"town > {spec.min_town_pop} inhabitants (prohibited)", "geometry": g} for g in tb.geometry],
            geometry="geometry", crs=L93)
    gdf["n"] = 1
    out = gdf.dissolve(by="kind", aggfunc={"n": "sum"}).reset_index().rename(columns={"n": "features"})
    out["area_km2"] = (out.area / 1e6).round(2)
    return out.to_crs("EPSG:4326")


# ---------------------------------------------------------------------------
# checker
# ---------------------------------------------------------------------------

def check(R: RoadArrays, its: list[Itinerary], spec: ExerciseSpec, zones: dict, towns_big) -> list[tuple[str, bool, str]]:
    """Re-verify every exercise constraint from the geometry (independent of how the routes were built)."""
    import shapely

    out = []
    out.append((f"{spec.k} itineraries found", len(its) == spec.k, f"{len(its)} found"))
    for i in range(len(its)):
        for j in range(i + 1, len(its)):
            a, b = its[i], its[j]
            dist = float(shapely.distance(a.line, b.line))
            out.append((f"{a.id} / {b.id} at least {spec.min_sep_m / 1000:g} km apart", dist >= spec.min_sep_m - 1e-6, f"min distance {dist / 1000:.2f} km"))
            if spec.no_intersect:
                inter = bool(shapely.intersects(a.line, b.line))
                out.append((f"{a.id} / {b.id} do not intersect", not inter, "intersect" if inter else "disjoint"))
    zo, zd = zones[spec.origin_zone], zones[spec.destination_zone]
    for it in its:
        s, e = shapely.get_point(it.line, 0), shapely.get_point(it.line, -1)
        out.append((f"{it.id} starts in the {spec.origin_zone} zone", bool(zo.contains(s)), ""))
        out.append((f"{it.id} ends in the {spec.destination_zone} zone", bool(zd.contains(e)), ""))
        if towns_big is not None and len(towns_big):
            bad = [t for t, g in zip(towns_big["town"], towns_big.to_crs(L93).geometry) if g.intersects(it.line)]
            out.append((f"{it.id} avoids towns > {spec.min_town_pop}", not bad, ", ".join(bad) or "none entered"))
        blocked = int((~R.ok[it.edge_idx]).sum())
        out.append((f"{it.id} uses only roads allowed for the column (SEG requirements)", blocked == 0, f"{blocked} blocked roads"))
    return out


__all__ = ["DistinctResult", "ExerciseSpec", "Itinerary", "RoadArrays", "Router", "TurnRouter", "check", "difficult_areas", "itinerary_facts",
           "parse_exercise", "solve_distinct", "turns_on"]

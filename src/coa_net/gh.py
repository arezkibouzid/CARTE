"""GraphHopper backend for NET.DISTINCT, to compare a production routing engine with the SciPy router.

Fair comparison by construction: GraphHopper gets the *same* graph and the *same* costs as ``exercise.Router``.

* ``export_osm`` writes the roads the column may use (``R.ok``) as an OSM PBF file: one one-way OSM way per directed
  road of ``R`` (way id = edge index + 1), junctions shared, interior points private to their way. The cost of a
  road (travel time times the T2/T3 penalty) becomes its ``maxspeed``, and the profile drives at ``max_speed``, so
  GraphHopper's travel time is the SciPy router's cost, up to GraphHopper's 2 km/h speed resolution.
* Zones are areas, GraphHopper routes between points: a hub node per zone is linked to every zone node by a
  connector way (``highway=service``, fast, high priority), so a hub-to-hub query is a zone-to-zone query.
  Connectors are dropped from the returned paths.
* The 5 km separation is GraphHopper-native: the roads near earlier itineraries are blocked by a per-request
  custom-model area (``priority 0 if in_excl``), the way ``exercise.solve_distinct`` masks them.
* First-itinerary seeds come from GraphHopper's own ``alternative_route`` algorithm (plateau alternatives), not
  from the via-node lattice of the SciPy router.

Paths come back as ``osm_way_id`` path details, i.e. as edge indices of ``R``, so the checker, the facts and the
outputs are the same for both engines. GraphHopper itself (Java 17+, Apache 2.0) runs as a local server started by
``GraphHopperServer``; nothing leaves the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .exercise import DistinctResult, Itinerary, RoadArrays, route_line

CONNECTOR_SPEED_KMH = 250  # just under GraphHopper's 254 km/h speed cap (7 bits, factor 2)
CONNECTOR_PRIORITY = 100.0  # connector cost divided by this: the hub-to-node detour is negligible

PROFILE = "coa"


def custom_model(excl: list | None = None) -> dict:
    """The profile's model: speed = the road's maxspeed (its cost), connectors cheap, ``excl`` polygons blocked."""
    cm = {
        "distance_influence": 0,
        # car_access carries the direction (every way is one-way): without this rule GraphHopper drives both ways
        "priority": [{"if": "!car_access", "multiply_by": "0"},
                     {"if": "road_class == SERVICE", "multiply_by": str(CONNECTOR_PRIORITY)}],
        "speed": [{"if": "true", "limit_to": "max_speed"}],
    }
    if excl:  # one area per polygon: GraphHopper rejects two features with the same id ("Unable to process JSON")
        ids = [f"excl{i}" for i in range(len(excl))]
        cond = " || ".join(f"in_{a}" for a in ids)
        cm["priority"].append({"if": f"({cond}) && road_class != SERVICE", "multiply_by": "0"})
        cm["areas"] = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": a, "properties": {}, "geometry": g} for a, g in zip(ids, excl)]}
    return cm


ORIENTATION_STEP_DEG = 12.0  # GraphHopper stores edge orientations on 5 bits, in steps of 12 degrees


def turn_penalty(turn_min: float = 1.0, sharp_deg: float = 75.0, sharp_min: float = 1.0) -> list[dict]:
    """``exercise.TurnRouter``'s turn cost as GraphHopper ``turn_penalty`` statements (seconds).

    GraphHopper's ``change_angle`` (degrees, signed: left/right) comes from orientations quantised to 12 degrees,
    so the deflection is a multiple of 12: each multiple gets the SciPy cost ``turn_min * (a / 90)**2`` (+
    ``sharp_min`` above ``sharp_deg``). Turns from or onto a zone connector are free. Deflections of 174 degrees
    and more are forbidden, like the SciPy router's U-turns (our ways are one-way, so GraphHopper's own U-turn
    rule never applies to a road and its reverse).
    """
    st = ORIENTATION_STEP_DEG
    out: list[dict] = [{"if": "road_class == SERVICE || prev_road_class == SERVICE", "add": "0"}]
    m = 0.0
    while m + st / 2 < 180.0:
        lo, hi = max(m - st / 2, 0.0), m + st / 2
        sec = 60.0 * (turn_min * (m / 90.0) ** 2 + (sharp_min if m > sharp_deg else 0.0))
        cond = (f"change_angle > -{hi:g} && change_angle < {hi:g}" if lo == 0 else
                f"(change_angle >= {lo:g} && change_angle < {hi:g}) || (change_angle <= -{lo:g} && change_angle > -{hi:g})")
        out.append({"else_if": cond, "add": f"{sec:.1f}"})
        m += st
    out.append({"else": "", "add": "Infinity"})
    return out


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@dataclass
class Export:
    path: Path
    hubs: dict[str, tuple[float, float]]  # "source"/"target" -> (lon, lat)
    n_edges: int  # way ids 1..n_edges are roads of R; larger ids are connectors
    ways: int
    speeds_kmh: tuple[float, float]


def export_osm(R: RoadArrays, path: str | Path, cost_min: np.ndarray, sources: np.ndarray, targets: np.ndarray,
               keep: np.ndarray | None = None) -> Export:
    """Write the roads ``keep`` (default ``R.ok``) and the two zone hubs as an OSM PBF file for GraphHopper."""
    import osmium

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    keep = R.ok if keep is None else keep
    G = R.G
    lonlat = np.array([(G.nodes[n]["x"], G.nodes[n]["y"]) for n in R.nodes])
    w = osmium.SimpleWriter(str(path))
    next_id = len(R.nodes) + 1
    nodes_out: dict[int, tuple[float, float]] = {}
    ways = []
    e_keep = np.flatnonzero(keep)
    speeds = R.length_m[e_keep] / 1000.0 / np.maximum(cost_min[e_keep], 1e-9) * 60.0
    speeds = np.clip(speeds, 2.0, CONNECTOR_SPEED_KMH - 10)
    for e, spd in zip(e_keep, speeds):
        a, b = R.edges[e]
        coords = G[a][b]["coords"]
        refs = [int(R.u[e]) + 1]
        nodes_out[refs[0]] = tuple(lonlat[R.u[e]])
        for p in coords[1:-1]:
            nodes_out[next_id] = (p[0], p[1])
            refs.append(next_id)
            next_id += 1
        refs.append(int(R.v[e]) + 1)
        nodes_out[refs[-1]] = tuple(lonlat[R.v[e]])
        ways.append((int(e) + 1, refs, {"highway": "unclassified", "oneway": "yes", "maxspeed": f"{spd:.0f}"}))
    hubs = {}
    wid = len(R.edges) + 1
    used = np.array(sorted(r - 1 for r in nodes_out if r <= len(R.nodes)))
    lo, hi = lonlat[used].min(axis=0) + 1e-5, lonlat[used].max(axis=0) - 1e-5  # GraphHopper rejects points on the bounds
    for role, zone_nodes in (("source", sources), ("target", targets)):
        zn = np.array(sorted(set(int(i) for i in zone_nodes) & set(used.tolist())))
        hub = _off_road(R, np.clip(lonlat[zn].mean(axis=0), lo, hi), lo, hi) if len(zn) else (0.0, 0.0)
        hub_id = next_id
        next_id += 1
        nodes_out[hub_id] = hub
        hubs[role] = hub
        for i in zn:
            refs = [hub_id, int(i) + 1] if role == "source" else [int(i) + 1, hub_id]
            ways.append((wid, refs, {"highway": "service", "oneway": "yes", "maxspeed": str(CONNECTOR_SPEED_KMH)}))
            wid += 1
    for nid in sorted(nodes_out):
        lon, lat = nodes_out[nid]
        w.add_node(osmium.osm.mutable.Node(id=nid, location=osmium.osm.Location(lon, lat), version=1))
    for way_id, refs, tags in ways:
        w.add_way(osmium.osm.mutable.Way(id=way_id, nodes=refs, tags=tags, version=1))
    w.close()
    return Export(path, hubs, len(R.edges), len(ways), (float(speeds.min()), float(speeds.max())))


def _off_road(R: RoadArrays, lonlat: np.ndarray, lo: np.ndarray, hi: np.ndarray, clear_m: float = 20.0) -> tuple:
    """The point nearest ``lonlat`` (spiral search, 25 m steps) at least ``clear_m`` from every road, so that
    GraphHopper snaps a query on the hub to the hub and not to a road through it."""
    import shapely
    from pyproj import Transformer

    from .realdata import L93

    fwd = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    back = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    x0, y0 = fwd.transform(*lonlat)
    for r in range(0, 4000, 25):
        for a in np.linspace(0, 2 * np.pi, max(1, r // 10), endpoint=False):
            x, y = x0 + r * np.cos(a), y0 + r * np.sin(a)
            lon, lat = back.transform(x, y)
            if not (lo[0] <= lon <= hi[0] and lo[1] <= lat <= hi[1]):
                continue
            p = shapely.Point(x, y)
            if not len(R.tree.query(p, predicate="dwithin", distance=clear_m)):
                return float(lon), float(lat)
    return float(lonlat[0]), float(lonlat[1])


# ---------------------------------------------------------------------------
# server
# ---------------------------------------------------------------------------

def default_home() -> Path:
    """Folder holding ``graphhopper-web-*.jar`` and a JDK (``COA_GH_HOME``; default ``../tools`` next to the repo)."""
    return Path(os.environ.get("COA_GH_HOME") or Path(__file__).resolve().parents[3] / "tools")


class GraphHopperServer:
    """A local GraphHopper server on one exported PBF file (graph imported into ``work/graph-cache``).

    ``turns``: ``turn_penalty`` statements (see ``turn_penalty``) for an edge-based profile that prices turns;
    the profile, not the request, carries them (a request's custom model is merged into the profile's)."""

    def __init__(self, pbf: str | Path, work: str | Path, home: str | Path | None = None, port: int = 8989,
                 heap: str = "4g", turns: list[dict] | None = None):
        self.turns = turns
        self.home = (Path(home) if home else default_home()).resolve()  # Java runs in ``work``: a relative home breaks
        self.pbf, self.work, self.port, self.heap = Path(pbf).resolve(), Path(work).resolve(), port, heap
        self.url = f"http://localhost:{port}"
        self.proc: subprocess.Popen | None = None
        self.import_s: float | None = None

    def _java(self) -> str:
        for name in ("java.exe", "java"):  # bin/ also holds java.dll
            for j in sorted(self.home.glob(f"jdk*/bin/{name}")):
                return str(j)
        return "java"

    def _jar(self) -> str:
        jars = sorted(self.home.glob("graphhopper-web-*.jar"))
        if not jars:
            raise FileNotFoundError(f"no graphhopper-web-*.jar in {self.home} (set COA_GH_HOME)")
        return str(jars[-1])

    def write_config(self) -> Path:
        self.work.mkdir(parents=True, exist_ok=True)
        cm = custom_model()
        ev = "car_access, road_access, max_speed, road_class, osm_way_id"
        turn_cfg = ""
        if self.turns:
            cm["turn_penalty"] = self.turns
            ev += ", orientation"
            turn_cfg = "\n      turn_costs:\n        vehicle_types: [motorcar]"
        (self.work / "coa.json").write_text(json.dumps(cm), encoding="utf-8")
        cfg = f"""graphhopper:
  datareader.file: {self.pbf.resolve().as_posix()}
  graph.location: {(self.work / 'graph-cache').resolve().as_posix()}
  graph.encoded_values: {ev}
  import.osm.ignored_highways: footway,construction,cycleway,path,steps
  custom_models.directory: {self.work.resolve().as_posix()}
  profiles:
    - name: {PROFILE}
      custom_model_files: [coa.json]{turn_cfg}
  profiles_ch: []
  profiles_lm: []
  routing.non_ch.max_waypoint_distance: 100000000
  routing.max_visited_nodes: 100000000
  prepare.min_network_size: 0
server:
  application_connectors:
    - type: http
      port: {self.port}
      bind_host: localhost
  admin_connectors:
    - type: http
      port: {self.port + 1}
      bind_host: localhost
"""
        p = self.work / "config.yml"
        p.write_text(cfg, encoding="utf-8")
        return p

    def start(self, timeout_s: float = 600.0) -> "GraphHopperServer":
        import requests

        cfg = self.write_config()
        log = open(self.work / "graphhopper.log", "w", encoding="utf-8")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen([self._java(), f"-Xmx{self.heap}", "-jar", self._jar(), "server", str(cfg)],
                                     stdout=log, stderr=subprocess.STDOUT, cwd=str(self.work))
        while time.perf_counter() - t0 < timeout_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"GraphHopper exited ({self.proc.returncode}); see {self.work / 'graphhopper.log'}")
            try:
                if requests.get(self.url + "/health", timeout=2).ok:
                    self.import_s = time.perf_counter() - t0
                    return self
            except requests.RequestException:
                pass
            time.sleep(1.0)
        self.stop()
        raise TimeoutError("GraphHopper did not start in time")

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()


# ---------------------------------------------------------------------------
# client and solver
# ---------------------------------------------------------------------------

class GHRouter:
    """Zone-to-zone paths from a GraphHopper server, as edge-index lists of ``R`` (connectors removed)."""

    def __init__(self, url: str, export: Export, timeout_s: float = 120.0, exploration: float = 3.0):
        import requests

        self.url, self.ex, self.timeout = url.rstrip("/"), export, timeout_s
        # alternative_route.max_exploration_factor. Exercise 1: node-based, 3.0 -> 20 paths in 2.6 s (1.0 -> 1 path);
        # edge-based (turn costs), 3.0 -> 52 s and > 120 s with exclusion areas, 1.5 -> 19 paths in 1.9 s
        self.exploration = exploration
        self.session = requests.Session()
        self.calls = 0
        self.seconds = 0.0

    def _query(self, excl: list | None, alternatives: int = 0, max_weight_factor: float = 1.6,
               max_share_factor: float = 0.6, max_exploration_factor: float | None = None) -> list[list[int]]:
        max_exploration_factor = max_exploration_factor or self.exploration
        body = {"points": [list(self.ex.hubs["source"]), list(self.ex.hubs["target"])], "profile": PROFILE,
                "ch.disable": True, "custom_model": custom_model(excl), "points_encoded": False,
                "instructions": False, "details": ["osm_way_id"]}
        if alternatives > 1:
            body.update({"algorithm": "alternative_route", "alternative_route.max_paths": alternatives,
                         "alternative_route.max_weight_factor": max_weight_factor,
                         "alternative_route.max_share_factor": max_share_factor,
                         # the default explores too little of the graph on a 60 km trip: 1 alternative instead of 20
                         "alternative_route.max_exploration_factor": max_exploration_factor})
        t0 = time.perf_counter()
        r = self.session.post(self.url + "/route", json=body, timeout=self.timeout)
        self.seconds += time.perf_counter() - t0
        self.calls += 1
        if r.status_code == 400 and "connection between locations not found" in r.text.lower():
            return []
        if not r.ok:
            dump = self.ex.path.parent / "failed_request.json"
            dump.write_text(json.dumps(body, indent=1), encoding="utf-8")
            raise RuntimeError(f"GraphHopper {r.status_code}: {r.text[:500]} (request in {dump})")
        out = []
        for p in r.json().get("paths", []):
            ids = [int(v) for _, _, v in p["details"]["osm_way_id"]]
            out.append([i - 1 for i in ids if 1 <= i <= self.ex.n_edges])
        return out

    def shortest(self, excl: list | None) -> list[int] | None:
        p = self._query(excl)
        return p[0] if p else None

    def alternatives(self, excl: list | None, n: int) -> list[list[int]]:
        return self._query(excl, alternatives=n)


def exclusion_polygon(R: RoadArrays, edges: list[int], sep_m: float) -> dict:
    """GeoJSON (WGS84) polygon of the band within ``sep_m`` of the roads ``edges``, enlarged by the two simplification
    tolerances (and 20 m) so that the roads the SciPy solver masks (within ``sep_m``) are all inside it. The route is
    simplified before buffering: buffering the raw centreline (thousands of vertices) dominated the solve time."""
    import shapely
    from pyproj import Transformer

    from .realdata import L93

    line_tol, poly_tol = 20.0, 40.0
    band = route_line(R, edges).simplify(line_tol).buffer(sep_m + line_tol + poly_tol + 20.0, quad_segs=4).simplify(poly_tol)
    tr = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    band = shapely.transform(band, lambda xy: np.column_stack(tr.transform(xy[:, 0], xy[:, 1])))
    return json.loads(shapely.to_geojson(band))


def _flatten(geoms: list[dict]) -> list[dict]:
    out = []
    for g in geoms:
        if g["type"] == "Polygon":
            out.append(g)
        elif g["type"] == "MultiPolygon":
            out += [{"type": "Polygon", "coordinates": c} for c in g["coordinates"]]
    return out


def solve_distinct_gh(R: RoadArrays, router: GHRouter, k: int, sep_m: float, cost_min: np.ndarray, n_seeds: int = 20,
                      n_second: int = 3, time_budget_s: float = 300.0, turn_cost=None) -> DistinctResult:
    """``exercise.solve_distinct`` with GraphHopper as the path engine: GraphHopper alternatives as seeds, exclusion
    bands as custom-model areas, the cheapest feasible set of k itineraries (cost measured on ``R``; ``turn_cost``:
    a ``TurnRouter`` whose turn costs are added, as ``solve_distinct`` does with a turn-aware router)."""
    import shapely

    t0 = time.perf_counter()
    log: list[str] = []
    spent = {"bands": 0.0, "separation check": 0.0}
    bands: dict[tuple, list[dict]] = {}

    def cost(edges: list[int]) -> float:
        c = float(cost_min[edges].sum())
        if turn_cost is not None:
            c += float(np.nansum(turn_cost.turn_cost_of(edges)))
        return c

    def band(edges: list[int]) -> list[dict]:
        key = tuple(edges)
        if key not in bands:
            t = time.perf_counter()
            bands[key] = _flatten([exclusion_polygon(R, edges, sep_m)])
            spent["bands"] += time.perf_counter() - t
        return bands[key]

    def sep_ok(chosen: list[list[int]]) -> bool:  # exact check on R: GraphHopper's area test is on simplified bands
        t = time.perf_counter()
        lines = [route_line(R, e) for e in chosen]
        ok = all(lines[i].distance(lines[j]) >= sep_m for i in range(len(lines)) for j in range(i))
        spent["separation check"] += time.perf_counter() - t
        return ok

    seeds = router.alternatives(None, n_seeds)
    log.append(f"{len(seeds)} first-itinerary seeds from GraphHopper alternative_route (within 1.6x, share <= 0.6, "
               f"exploration factor {router.exploration:g})")
    sets: list[tuple[float, list[list[int]]]] = []
    tried = rejected = 0
    for si, e1 in enumerate(seeds):
        if time.perf_counter() - t0 > time_budget_s:
            log.append(f"time budget reached after {si} seeds")
            break
        if not e1:
            continue
        excl1 = band(e1)
        if k == 1:
            sets.append((cost(e1), [e1]))
            continue
        for e2 in router.alternatives(excl1, n_second) if n_second > 1 else [router.shortest(excl1)]:
            if not e2:
                continue
            tried += 1
            chosen, excl, ok = [e1, e2], excl1 + band(e2), True
            for _ in range(k - 2):
                e = router.shortest(excl)
                if not e:
                    ok = False
                    break
                chosen.append(e)
                excl = excl + band(e)
            if ok and sep_ok(chosen):
                sets.append((sum(cost(e) for e in chosen), chosen))
            elif ok:
                rejected += 1
    uniq: dict[tuple, tuple[float, list[list[int]]]] = {}
    for c, es in sets:
        uniq.setdefault(tuple(sorted(tuple(e) for e in es)), (c, es))
    sets = sorted(uniq.values(), key=lambda t: t[0])
    log.append(f"{tried} completions tried, {len(sets)} distinct feasible sets of {k}"
               + (f", {rejected} rejected by the exact separation check" if rejected else ""))
    log.append(f"{router.calls} GraphHopper queries, {router.seconds:.1f} s inside GraphHopper; "
               + ", ".join(f"{k} {v:.1f} s" for k, v in spent.items()))
    its: list[Itinerary] = []
    if sets:
        best = sets[0][1]
        lines = [route_line(R, e) for e in best]
        order = sorted(range(len(best)), key=lambda i: shapely.line_interpolate_point(lines[i], 0.5, normalized=True).x)
        for j, i in enumerate(order):
            e = best[i]
            nodes = [R.nodes[R.u[e[0]]]] + [R.nodes[R.v[x]] for x in e]
            its.append(Itinerary(id=f"IT-{j + 1}", nodes=nodes, edge_idx=e, time_min=float(R.time_min[e].sum()),
                                 length_m=float(R.length_m[e].sum()), line=lines[i]))
    alts = [(t, [float(R.time_min[e].sum()) for e in es]) for t, es in sets[1:6]]
    return DistinctResult(its, tried, len(sets), time.perf_counter() - t0, log, alts)


def gh_variant(work: str | Path, home: str | Path | None = None, port: int = 8989, n_seeds: int = 20,
               turns: bool = False):
    """A solver for ``scenario.run_exercise(extra_solvers=...)``: export, start GraphHopper, solve, stop.

    ``turns``: price junction turns like the turn-aware SciPy router (``spec.turn_penalty_min``, ``sharp_turn_deg``,
    ``sharp_turn_min``), in GraphHopper's search (``turn_penalty``) and in the selection of the best set."""

    def solve(R: RoadArrays, src: np.ndarray, dst: np.ndarray, pen: np.ndarray, spec) -> DistinctResult:
        from .exercise import TurnRouter

        cost = R.time_min * pen
        t0 = time.perf_counter()
        ex = export_osm(R, Path(work) / "roads.osm.pbf", cost, src, dst)
        t_export = time.perf_counter() - t0
        import shutil

        shutil.rmtree(Path(work) / "graph-cache", ignore_errors=True)  # the export changes with the exercise
        tp = turn_penalty(spec.turn_penalty_min, spec.sharp_turn_deg, spec.sharp_turn_min) if turns else None
        pricer = TurnRouter(R, src, dst, pen, turn_min=spec.turn_penalty_min, sharp_deg=spec.sharp_turn_deg,
                            sharp_min=spec.sharp_turn_min) if turns else None
        with GraphHopperServer(ex.path, work, home=home, port=port, turns=tp) as srv:
            router = GHRouter(srv.url, ex, exploration=1.5 if turns else 3.0)
            res = solve_distinct_gh(R, router, spec.k, spec.min_sep_m, cost, n_seeds=n_seeds,
                                    turn_cost=pricer)
        res.log.insert(0, f"export {ex.ways} OSM ways in {t_export:.1f} s (speeds {ex.speeds_kmh[0]:.0f}-"
                          f"{ex.speeds_kmh[1]:.0f} km/h), GraphHopper import and start {srv.import_s:.1f} s"
                          + (", turns priced (turn_penalty, edge-based)" if turns else ""))
        return res

    return solve

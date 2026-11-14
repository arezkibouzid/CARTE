"""Constrained itineraries for the interface: the exercise pipeline of ``coa_net`` (NET.DISTINCT) on the app's networks.

    k itineraries from a departure zone to an arrival zone, pairwise at least d km apart (and not crossing),
    every road checked against the column's vehicle (``mission.build_context``: width, load, gauge, slope...),
    junction turns priced (turn-aware line graph), built-up roads penalised, zones to avoid closed;
    then difficult turns (curvature radius), difficult-manoeuvre areas and an independent constraint check.

Engine: SciPy's compiled Dijkstra (``scipy.sparse.csgraph``) on the junction graph or on the line graph (turns priced),
with the first-itinerary seeds explored on parallel threads (shapely's spatial queries release the GIL). CPU only.

The expensive part (graph, per-road evaluation for the vehicle, road arrays) is built once per
(sheet, network, vehicle) and kept in memory; a request then costs the route search only.
"""

from __future__ import annotations

import dataclasses
import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np

L93 = "EPSG:2154"
_bases: OrderedDict = OrderedDict()
_base_lock = threading.Lock()

VEHICLE_LABELS = {
    "pfm_carrier": "Porteur PFM (pont flottant motorisé)", "tank_transporter": "Porte-char", "heavy_truck": "Poids lourd",
    "medium_truck": "Camion moyen", "fuel_tanker": "Citerne carburant", "ifv_tracked": "Véhicule chenillé",
    "ambulance": "Ambulance", "light_utility": "Véhicule léger",
}


def capabilities() -> dict:
    from coa_net.vehicles import load_catalogue

    cat = load_catalogue()
    return {"vehicles": [{"id": k, "label": VEHICLE_LABELS.get(k, k), "width_m": cat[k].width_m} for k in sorted(cat)],
            "threads": min(8, os.cpu_count() or 1)}


# ---------------------------------------------------------------------------------------------------------- base

@dataclasses.dataclass
class Base:
    key: tuple
    G: object
    R: object  # exercise.RoadArrays
    evals: dict
    pen: np.ndarray
    vector: bool  # BD TOPO / OSM centre lines (precise radii) vs. network read from the scan
    width_need_m: float
    single_lane_m: float
    builtup: object
    seconds: float
    blocked: dict


BASE_VERSION = 1  # bump when _build_base or the coa_net costing changes what it builds


def get_base(key: tuple, graph_fn, builtup_fn, vehicle: str, vector: bool, disk: Path | None = None,
             source: str | Path | None = None) -> Base:
    """The prepared network, from memory, else from the disk cache ``disk`` (keyed by ``source``'s size and date),
    else built (about 30 s for the full BD TOPO) and written there."""
    import hashlib
    import pickle

    with _base_lock:
        if key in _bases:
            _bases.move_to_end(key)
            return _bases[key]
        f = None
        if disk is not None and source is not None:
            from coa_net.scenario import file_key

            h = hashlib.sha1(f"{key}|{file_key(source)}|{BASE_VERSION}".encode()).hexdigest()[:16]
            f = Path(disk) / f"base_{'_'.join(map(str, key))}_{h}.pkl"
        b = None
        if f is not None and f.exists():
            t0 = time.perf_counter()
            try:
                with f.open("rb") as fh:
                    b = pickle.load(fh)
                b.seconds = round(time.perf_counter() - t0, 1)
            except Exception:  # noqa: BLE001 - a broken cache file is rebuilt
                b = None
        if b is None:
            b = _build_base(key, graph_fn, builtup_fn, vehicle, vector)
            if f is not None:
                f.parent.mkdir(parents=True, exist_ok=True)
                tmp = f.with_suffix(".tmp")
                with tmp.open("wb") as fh:
                    pickle.dump(b, fh, protocol=pickle.HIGHEST_PROTOCOL)
                tmp.replace(f)
        _bases[key] = b
        while len(_bases) > 3:  # a BD TOPO base holds ~300 000 directed roads: keep a few only
            _bases.popitem(last=False)
        return b


def _build_base(key, graph_fn, builtup_fn, vehicle: str, vector: bool) -> Base:
    from collections import Counter

    from coa_net.corridors import CorridorGraph
    from coa_net.exercise import RoadArrays, edge_lines, parse_exercise
    from coa_net.mission import build_context
    from coa_net.parsing import parse_order
    from coa_net.realdata import systemic_unknowns
    from coa_net.requirements import load_requirements
    from coa_net.scanlearn import uncertainty_penalty
    from coa_net.scenario import annotate_areas
    from coa_net.vehicles import load_catalogue

    t0 = time.perf_counter()
    G = graph_fn()
    edges = list(G.edges)
    lines = edge_lines(G, edges)
    builtup = builtup_fn()
    annotate_areas(G, lines, edges, builtup, None)
    # the order names its places: two junctions carry the zone names (the per-road evaluation does not depend on them)
    nodes = list(G.nodes)
    G.nodes[nodes[0]]["name"], G.nodes[nodes[-1]]["name"] = "Origin zone", "Destination zone"
    cat = load_catalogue()
    _, ex_reqs = parse_exercise("Determine 2 itineraries at least 1 km apart; they must not intersect.")
    reqs, _ = systemic_unknowns(G, load_requirements() + ex_reqs)
    order = (f"ORDER APP\nPhase: day\nIntent: separate itineraries.\nConvoy C1: 1 x {vehicle} from Origin zone to "
             "Destination zone, depart 06:00, priority 1.\n")
    ctx = build_context(CorridorGraph(G, max_rank=4 if vector else 5), parse_order(order, G, cat), reqs, cat)
    grp = next(iter(ctx.groups.values()))
    evals = grp.coster.evals
    R = RoadArrays.build(G, evals, lines)
    # T2/T3 preferences priced as in the exercise pipeline: built-up and possibly-too-narrow roads cost more
    pen = np.ones(len(R.edges))
    for i, e in enumerate(R.edges):
        ev = evals[e]
        if ev.builtup:
            pen[i] *= 1.5
        if any("may be under" in d for _, d in ev.unknown):
            pen[i] *= 1.5
        elif ev.pref:
            pen[i] *= 1.2
        if G.edges[e].get("source_id") == "scan":
            pen[i] *= uncertainty_penalty(G.edges[e])
    blocked = Counter(re.sub(r"[\d.]+ ?(%|m|t)?", "", d.split("(")[0]).strip()
                      for ev in evals.values() if not ev.ok for _, _, d in ev.blocked[:1])
    w = cat[vehicle].width_m
    return Base(key=key, G=G, R=R, evals=evals, pen=pen, vector=vector, width_need_m=2 * w + 0.5, single_lane_m=w + 1.0,
                builtup=builtup, seconds=round(time.perf_counter() - t0, 1), blocked=dict(blocked.most_common(6)))


# ---------------------------------------------------------------------------------------------------------- solve

def _poly_l93(ring):
    import shapely
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    x, y = tr.transform([p[0] for p in ring], [p[1] for p in ring])
    return shapely.make_valid(shapely.Polygon(list(zip(x, y))))


def _disk_l93(pt, radius_m):
    import shapely
    from pyproj import Transformer

    x, y = Transformer.from_crs("EPSG:4326", L93, always_xy=True).transform(pt[0], pt[1])
    return shapely.Point(x, y).buffer(radius_m)


def _to_lonlat(geom):
    import geopandas as gpd

    return gpd.GeoSeries([geom], crs=L93).to_crs("EPSG:4326").iloc[0]


CHECK_FR = [
    (r"^(\d+) itineraries found$", r"\1 itinéraires trouvés"),
    (r"^(IT-\d+) / (IT-\d+) at least ([\d.]+) km apart$", r"\1 / \2 à au moins \3 km l’un de l’autre"),
    (r"^(IT-\d+) / (IT-\d+) do not intersect$", r"\1 / \2 ne se croisent pas"),
    (r"^(IT-\d+) starts in the Origin zone$", r"\1 part de la zone de départ"),
    (r"^(IT-\d+) ends in the Destination zone$", r"\1 arrive dans la zone d’arrivée"),
    (r"^(IT-\d+) uses only roads allowed for the column.*$", r"\1 n’emprunte que des routes autorisées pour le convoi"),
    (r"^(IT-\d+) avoids towns.*$", r"\1 évite les grandes villes"),
]
DETAIL_FR = [(r"^(\d+) found$", r"\1 trouvés"), (r"^min distance ([\d.]+) km$", r"distance minimale \1 km"),
             (r"^disjoint$", "disjoints"), (r"^intersect$", "se croisent"), (r"^(\d+) blocked roads$", r"\1 route(s) interdite(s)")]


def _fr(s: str, table) -> str:
    for pat, rep in table:
        if re.match(pat, s):
            return re.sub(pat, rep, s)
    return s


def solve(base: Base, origin, destination, k: int, sep_km: float, no_intersect: bool, turn_aware: bool,
          avoid: list, area, n_seeds: int) -> dict:
    """``origin``/``destination``: Lambert-93 polygons. Returns GeoJSON layers, facts, checks and the decision log."""
    import shapely

    from coa_net.exercise import ExerciseSpec, Router, TurnRouter, check, difficult_areas, itinerary_facts, solve_distinct, turns_on
    from coa_net.scenario import mark_junctions

    T: dict[str, float] = {}
    t = time.perf_counter()
    R, G = base.R, base.G
    spec = ExerciseSpec(k=k, min_sep_m=sep_km * 1000.0, no_intersect=no_intersect, min_town_pop=None,
                        origin_zone="Origin", destination_zone="Destination")
    log = [f"réseau : {len(R.edges)} tronçons orientés, {int(R.ok.sum())} autorisés pour le véhicule"
           + (f" (préparé en {base.seconds} s)" if base.seconds else "")]
    if base.blocked:
        log.append("routes interdites (première raison) : " + ", ".join(f"{k_} ×{n}" for k_, n in base.blocked.items()))
    ok = R.ok.copy()
    if area is not None:  # deployment area: roads leaving it are closed
        inside = np.zeros(len(R.edges), bool)
        inside[R.tree.query(area, predicate="contains")] = True  # STRtree: predicate(area, road), i.e. roads inside the area
        log.append(f"zone de déploiement : {int((ok & ~inside).sum())} tronçons fermés")
        ok &= inside
    for z in avoid:
        hit = R.tree.query(z, predicate="intersects")
        log.append(f"zone à éviter : {int(ok[hit].sum())} tronçons fermés")
        ok[hit] = False
    Rq = dataclasses.replace(R, ok=ok)
    okn = np.zeros(len(R.nodes), bool)
    okn[R.u[ok]] = True
    okn[R.v[ok]] = True
    pts = shapely.points(R.node_xy)
    src = np.flatnonzero(okn & shapely.contains(origin, pts))
    dst = np.flatnonzero(okn & shapely.contains(destination, pts))
    log.append(f"carrefours utilisables : {len(src)} dans la zone de départ, {len(dst)} dans la zone d’arrivée")
    if not len(src) or not len(dst):
        raise ValueError("Aucune route utilisable dans la zone de " + ("départ" if not len(src) else "arrivée")
                         + " : agrandissez-la ou déplacez-la.")
    zones = {"Origin": origin, "Destination": destination}
    widths = [float(np.sqrt(z.area)) for z in zones.values()]
    if k > 1 and min(widths) < sep_km * 1000:
        log.append(f"attention : une zone fait environ {min(widths) / 1000:.1f} km de large, moins que l’écart demandé "
                   f"({sep_km:g} km) ; des itinéraires distincts peuvent être impossibles à faire partir ou arriver")
    T["préparation"] = time.perf_counter() - t
    t = time.perf_counter()

    router = (TurnRouter(Rq, src, dst, base.pen, turn_min=spec.turn_penalty_min, sharp_deg=spec.sharp_turn_deg,
                         sharp_min=spec.sharp_turn_min) if turn_aware else Router(Rq, src, dst, base.pen))
    res = solve_distinct(Rq, src, dst, k, spec.min_sep_m, n_seeds=n_seeds, penalty=base.pen, router=router, time_budget_s=240.0)
    log.append("recherche : Dijkstra compilé (SciPy)" + (" sur le graphe des tronçons, virages aux carrefours tarifés" if turn_aware else "")
               + f", {min(8, os.cpu_count() or 1)} fils d’exécution en parallèle")
    T["recherche"] = time.perf_counter() - t
    t = time.perf_counter()
    log += res.log
    if not res.itineraries:
        raise ValueError(f"Aucun ensemble de {k} itinéraire(s) à au moins {sep_km:g} km les uns des autres. "
                         "Agrandissez les zones, réduisez l’écart ou le nombre d’itinéraires.\n" + "\n".join(res.log))

    half_chord = 10.0 if base.vector else 20.0
    feats, turns = [], []
    for i, it in enumerate(res.itineraries):
        f = itinerary_facts(R, it, base.evals, None, spec, need_width_m=base.width_need_m)
        it.turns = turns_on(it.line, spec.turn_radius_warn_m, spec.turn_radius_crit_m, half_chord_m=half_chord)
        it.facts = f
        mark_junctions(G, R, it)
        g = _to_lonlat(it.line)
        props = {"id": it.id, "index": i, "length_km": f["length_km"], "time_min": f["time_min"], "roads": f["roads"][:12],
                 "bridges": f["bridges"], "tunnels": f["tunnels"], "builtup_km": f["builtup_km"], "narrow_km": f["narrow_km"],
                 "width_unknown_km": f["width_unknown_km"], "steep_km": f["steep_km"],
                 "turns_difficult": sum(1 for x in it.turns if x["severity"] == "difficult"),
                 "turns_critical": sum(1 for x in it.turns if x["severity"] == "critical"),
                 "min_radius_m": min((x["radius_m"] for x in it.turns), default=None)}
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "LineString", "coordinates": np.round(np.asarray(g.coords), 6).tolist()}})
        for j, tr in enumerate(it.turns, start=1):
            turns.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(tr["lon"], 6), round(tr["lat"], 6)]},
                          "properties": {"itinerary": it.id, "index": i, "turn": f"{it.id}-V{j}", "radius_m": tr["radius_m"],
                                         "deflection_deg": tr["deflection_deg"], "severity": tr["severity"],
                                         "at_junction": tr.get("at_junction"), "km": tr["km"]}})
    checks = [{"label": _fr(lbl, CHECK_FR), "ok": bool(okk), "detail": _fr(det, DETAIL_FR)}
              for lbl, okk, det in check(Rq, res.itineraries, spec, zones, None)]
    T["faits et contrôle"] = time.perf_counter() - t
    t = time.perf_counter()

    # difficult-manoeuvre areas along the itineraries (a 3 km band), not over the whole sheet
    band = shapely.union_all([it.line for it in res.itineraries]).buffer(3000)
    extra = [(f"virage critique sur un itinéraire (rayon < {spec.turn_radius_crit_m:g} m)", shapely.Point(x["x"], x["y"]))
             for it in res.itineraries for x in it.turns if x["severity"] == "critical"]
    areas = difficult_areas(R, band, spec, None, base.builtup, need_width_m=base.width_need_m,
                            single_lane_m=base.single_lane_m, extra=extra)
    areas["geometry"] = areas.geometry.simplify(0.0002)
    hard = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": _kind_fr(kd), "features": int(n), "area_km2": float(a)},
         "geometry": shapely.geometry.mapping(g)}
        for kd, n, a, g in zip(areas["kind"], areas["features"], areas["area_km2"], areas.geometry) if g is not None and not g.is_empty]}
    T["zones difficiles"] = time.perf_counter() - t

    zfc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": "from" if z == "Origin" else "to"}, "geometry": shapely.geometry.mapping(_to_lonlat(g))}
        for z, g in zones.items()]}
    return {"itineraries": {"type": "FeatureCollection", "features": feats}, "turns": {"type": "FeatureCollection", "features": turns},
            "difficult_areas": hard, "zones": zfc, "checks": checks, "all_ok": all(c["ok"] for c in checks), "log": log,
            "solver": {"turn_aware": turn_aware, "tried": res.tried, "feasible_sets": res.feasible_sets,
                       "alternatives": len(res.alternatives)},
            "timing_s": {k_: round(v, 1) for k_, v in T.items()}}


KIND_FR = [(r"^bridge \(load class unknown\)$", "pont (classe de charge inconnue)"), (r"^bridge$", "pont"),
           (r"^tunnel \(clearance unknown\)$", "tunnel (hauteur libre inconnue)"), (r"^tunnel$", "tunnel"), (r"^ford$", "gué"),
           (r"^carriageway < ([\d.]+) m$", r"chaussée < \1 m"), (r"^slope > ([\d.]+) %$", r"pente > \1 %"),
           (r"^bend radius < ([\d.]+) m$", r"virage de rayon < \1 m"),
           (r"^built-up area.*$", "zone bâtie (rues étroites, circulation)")]


def _kind_fr(s: str) -> str:
    return _fr(s, KIND_FR)

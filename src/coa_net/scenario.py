"""End-to-end run on the organisers' Scenario data (exercise mode), with timings and sanity checks.

    shapefiles -> graph (BD TOPO primary, OSM for cross-checks)
    1:100 000 scan -> built-up areas, town names and populations (OCR)
    exercise sketch -> georeferenced zones
    exercise text -> spec + requirement instances
    order -> COA-Net mission context -> per-road evaluation (SEG masks)
    NET.DISTINCT solver -> itineraries -> turns, facts, difficult areas -> checker
    outputs: GeoJSON layers, map overlays (PNG), decision log, metrics.json
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .exercise import (RoadArrays, Router, TurnRouter, check, difficult_areas, edge_lines, itinerary_facts, parse_exercise, route_line,
                       solve_distinct, turns_on)
from .realdata import L93, load_scenario_bdtopo

log = logging.getLogger(__name__)

EXERCISE_1 = """The unit needs to get a mobile floatable bridge (called « Pont Flottant Motorisé » in French) from the South zone to the North zone. Determine 3 itineraries that link the two delimited zones with the following constraints.
- Avoid getting in towns of more than 10k inhabitants;
- Each itineraries must not intersect in any way and be at least 5km apart);
Desired output :
- Layer on top of the map including the different itineraries;
- Layer on top of the map with all the potential areas where maneuvering is difficult;
- GeoJSON file with the itineraries as well as the location and curvature radius for potential difficult turns for the convoy on said itineraries;
- A text file describing the decision process."""


@dataclass
class ScenarioPaths:
    bdtopo: str
    osm: str | None = None
    scan100: str | None = None
    scan250: str | None = None
    sketch: str | None = None

    @classmethod
    def from_dir(cls, d: str | Path, sketch: str | None = None) -> "ScenarioPaths":
        d = Path(d)

        def find(*names):
            for n in names:
                for p in d.glob(n):
                    return str(p)
            return None

        return cls(bdtopo=find("*BDTopo*.shp", "*BDTOPO*.shp"), osm=find("*OSM*.shp"),
                   scan100=find("*100K*.tif", "*100k*.tif"), scan250=find("*250K*.tif", "*250k*.tif"), sketch=sketch)


@dataclass
class ScenarioRun:
    out: Path
    timings: dict[str, float] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)

    def tick(self, key: str, t0: float) -> float:
        t = time.perf_counter()
        self.timings[key] = round(t - t0, 2)
        print(f"  [{key}] {t - t0:.1f} s", flush=True)
        return t


def exercise_from_pdf(pdf: str | Path, out_dir: str | Path, with_reading: bool = False):
    """Statement and illustration of an exercise document in the organisers' format (``orderdoc.read_order``): the text in
    reading order, and the largest figure (the map illustration with the zones), written to ``out_dir`` with the text
    and the document's blocks. Returns (text, sketch path), plus the reading with ``with_reading``."""
    from .orderdoc import read_order

    reading = read_order(pdf)
    fig = reading.illustration
    if fig is None or fig.data is None:
        raise ValueError(f"{pdf}: no image found; the exercise sketch is needed for the zones (give it with --sketch)")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / ("sketch" + (fig.ext or ".png"))
    path.write_bytes(fig.data)
    text = reading.text
    (out / "exercise.txt").write_text(text, encoding="utf-8")
    (out / "blocks.json").write_text(json.dumps([{"id": i.block, "role": i.role, "page": i.page, "text": i.text} for i in reading.items],
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    return (text, str(path), reading) if with_reading else (text, str(path))


GRAPH_CACHE_VERSION = 5  # bump when realdata.py changes the graph it builds


def file_key(path: str | Path, content: bool = False) -> str:
    """Short key of an input file for cache names: its path, size and date, or its bytes (small files such as a sketch)."""
    import hashlib

    p = Path(path)
    if content:
        return hashlib.sha1(p.read_bytes()).hexdigest()[:12]
    st = p.stat()
    return hashlib.sha1(f"{p.resolve()}|{st.st_size}|{int(st.st_mtime)}".encode()).hexdigest()[:12]


def load_graph_cached(path: str, cache: Path | None, kind: str = "bdtopo"):
    """The road graph with a pickle cache keyed by the file's size and date and the loader version.

    ``kind``: ``"bdtopo"`` (BD TOPO shapefile, :func:`load_scenario_bdtopo`) or ``"scan"`` (road network read
    from a scanned map, :func:`coa_net.scanroads.load_scenario_scan`). The cache is this program's own output,
    in the run's cache folder; never point it at files from elsewhere."""
    import pickle

    from .scanroads import SCAN_LOADER_VERSION, load_scenario_scan

    def load():
        if kind == "scan":
            G, rep, _ = load_scenario_scan(path)
            return G, rep
        if kind == "osm":
            from .realdata import load_scenario_osm

            return load_scenario_osm(path)
        return load_scenario_bdtopo(path)

    if cache is None:
        G, rep = load()
        return G, rep, False
    version = f"s{SCAN_LOADER_VERSION}" if kind == "scan" else str(GRAPH_CACHE_VERSION)
    f = Path(cache) / f"{kind}_graph_{file_key(path)}_v{version}.pkl"
    if f.exists():
        with f.open("rb") as fh:
            G, rep = pickle.load(fh)
        return G, rep, True
    G, rep = load()
    with f.open("wb") as fh:
        pickle.dump((G, rep), fh, protocol=pickle.HIGHEST_PROTOCOL)
    return G, rep, False


def annotate_areas(G, lines: np.ndarray, edges: list, builtup, towns_big) -> dict:
    """Edge flags from the scan: ``built_up`` (midpoint in a built-up polygon), ``town``/``town_pop`` (edge enters a big town)."""
    import geopandas as gpd
    import shapely

    mids = shapely.line_interpolate_point(lines, 0.5, normalized=True)
    pts = gpd.GeoDataFrame({"i": np.arange(len(edges))}, geometry=mids, crs=L93)
    counts = {"built_up": 0, "town": 0}
    if builtup is not None and len(builtup):
        j = gpd.sjoin(pts, builtup.to_crs(L93)[["geometry"]], predicate="within", how="inner")
        for i in j["i"].unique():
            a, b = edges[i]
            G[a][b]["built_up"] = True
            counts["built_up"] += 1
    if towns_big is not None and len(towns_big):
        tree = shapely.STRtree(lines)
        tb = towns_big.to_crs(L93)
        for name, pop, geom in zip(tb["town"], tb["population"], tb.geometry):
            for i in tree.query(geom, predicate="intersects"):
                a, b = edges[i]
                G[a][b]["town"], G[a][b]["town_pop"] = name, int(pop)
                counts["town"] += 1
    return counts


def run_exercise(paths: ScenarioPaths, out: str | Path, exercise_text: str = EXERCISE_1, cache: str | Path | None = None,
                 n_seeds: int = 20, max_rank: int | None = None, turn_aware: bool = True, compare: bool = True,
                 osm_check: bool = True, llm=None, requirements: list | None = None, network: str = "scan",
                 corrector: str | Path | None = None, extra_solvers: dict | None = None) -> ScenarioRun:
    """Solve an itineraries exercise. ``llm``: model for the sketch reader (L9, needs images); ``requirements``: the
    requirement base (default: the seed base; give the base compiled from the event's doctrine). ``extra_solvers``:
    name -> ``f(R, src, dst, pen, spec) -> DistinctResult``, other NET.DISTINCT engines run on the same roads and
    costs and reported in ``router_comparison`` (e.g. ``gh.gh_variant``); the outputs keep the built-in solver.

    ``network``: where the road graph comes from - ``"scan"`` (read from the 1:100 000 scan, the image input the
    brief encourages; the shapefiles only check it), ``"scan250"`` (from the 1:250 000 scan) or ``"bdtopo"`` (the
    BD TOPO shapefile, as on main). ``max_rank`` defaults to 5 on a scan (the map draws BD TOPO importance 4 and 5
    roads with one white symbol, so it cannot tell them apart) and 4 on BD TOPO."""
    import geopandas as gpd
    import shapely

    from .corridors import CorridorGraph
    from .graph import nearest_node
    from .mission import build_context
    from .parsing import parse_order
    from .requirements import load_requirements
    from .figures import names_in, read_sketch
    from .scanmap import builtup_polygons, georeference_sketch, ocr_labels, reread_populations, town_areas, towns_from_labels
    from .vehicles import load_catalogue

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    cache = Path(cache) if cache else out / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    run = ScenarioRun(out)
    T = time.perf_counter()

    # 1. exercise text
    t = time.perf_counter()
    spec, ex_reqs = parse_exercise(exercise_text)
    run.facts["exercise"] = {"recognised": spec.recognised, "assumptions": spec.assumptions, "k": spec.k,
                             "min_sep_m": spec.min_sep_m, "min_town_pop": spec.min_town_pop, "outputs": spec.outputs}
    t = run.tick("parse exercise", t)

    from .scanlearn import uncertainty_penalty

    # 2. graph (parsed once, then read back from the cache: several exercises run on the same map)
    if network in ("scan", "scan250"):
        tif = paths.scan100 if network == "scan" else paths.scan250
        if not tif:
            raise ValueError(f"network '{network}' needs the scanned map ({'*100K.tif' if network == 'scan' else '*250K.tif'})")
        G, rep_bd, cached = load_graph_cached(tif, cache, kind="scan")
        run.facts["network"] = {"source": f"image: {Path(tif).name}", "scan_report": G.graph.get("scan_report", {}).get("seconds")}
        run.facts["load_scan"] = {k: v for k, v in rep_bd.__dict__.items()}
        t = run.tick("read the road network from the scan" + (" (from the cache)" if cached else ""), t)
        if corrector:
            from .scanlearn import correct_graph

            run.facts["network"]["corrector"] = correct_graph(G, tif, corrector)
            t = run.tick("learned corrector of the image network", t)
        max_rank = 5 if max_rank is None else max_rank
    elif network == "osm":
        if not paths.osm:
            raise ValueError("network 'osm' needs the OSM shapefile (*OSM*.shp)")
        G, rep_bd, cached = load_graph_cached(paths.osm, cache, kind="osm")
        run.facts["network"] = {"source": f"shapefile: {Path(paths.osm).name}"}
        run.facts["load_osm"] = rep_bd.__dict__
        t = run.tick("load OSM graph" + (" (from the cache)" if cached else ""), t)
        max_rank = 4 if max_rank is None else max_rank
    else:
        G, rep_bd, cached = load_graph_cached(paths.bdtopo, cache)
        run.facts["network"] = {"source": f"shapefile: {Path(paths.bdtopo).name}"}
        run.facts["load_bdtopo"] = rep_bd.__dict__
        t = run.tick("load BD TOPO graph" + (" (from the cache)" if cached else ""), t)
        max_rank = 4 if max_rank is None else max_rank
    run.facts["network"]["max_rank"] = max_rank
    # turn radius: chord of +-10 m on BD TOPO centre lines; a 1:100 000 scan (7 m pixels, generalised symbols) cannot
    # resolve such a short chord, so +-20 m (about three pixels) is used there and the radii are indicative only
    half_chord = 10.0 if network in ("bdtopo", "osm") else 20.0
    run.facts["network"]["turn_half_chord_m"] = half_chord
    if network not in ("bdtopo", "osm"):
        spec.assumptions[:] = [
            (f"difficult turn: centreline radius below {spec.turn_radius_warn_m:g} m, critical below {spec.turn_radius_crit_m:g} m "
             "(path length / heading change over a 40 m window of the centre line read on the scan; the map generalises roads "
             "and its pixels are about 7 m, so radii are indicative: a critical turn is a reconnaissance item)") if a.startswith("difficult turn")
            else ("steep: not assessed (the scan has contour lines but no slope is read from them in this version)") if a.startswith("steep")
            else a for a in spec.assumptions]

    # 3. perception on the scan
    builtup = towns = towns_big = None
    unresolved: list[str] = []
    if paths.scan100:
        scan_key = file_key(paths.scan100)
        bu_path = cache / f"builtup_{scan_key}.geojson"
        if bu_path.exists():
            builtup = gpd.read_file(bu_path)
        else:
            builtup = builtup_polygons(paths.scan100)
            builtup.to_file(bu_path, driver="GeoJSON")
        t = run.tick("built-up areas from the 1:100k scan", t)
        labels = ocr_labels(paths.scan100, cache=str(cache / f"labels_{scan_key}.json"))
        t = run.tick("OCR of the 1:100k scan", t)
        towns = towns_from_labels(labels)
        tw_path = cache / f"towns_{scan_key}.json"
        if tw_path.exists():
            data = json.loads(tw_path.read_text(encoding="utf-8"))
            from .scanmap import Town

            towns, unresolved = [Town(**d) for d in data["towns"]], data["unresolved"]
        else:
            towns, unresolved = reread_populations(paths.scan100, labels, towns)
            tw_path.write_text(json.dumps({"towns": [t_.__dict__ for t_ in towns], "unresolved": unresolved}, ensure_ascii=False, indent=1), encoding="utf-8")
        towns_big = town_areas(builtup, towns, spec.min_town_pop or 10**9) if spec.min_town_pop else None
        run.facts["towns"] = [(t_.name, t_.population) for t_ in towns]
        run.facts["towns_unresolved"] = unresolved
        run.facts["towns_big"] = [] if towns_big is None else list(zip(towns_big["town"], towns_big["population"]))
        run.facts["builtup"] = {"polygons": len(builtup), "area_km2": round(float(builtup.area_ha.sum()) / 100, 1)}
        t = run.tick("town populations and areas", t)

    # 4. zones from the sketch
    if not paths.sketch:
        raise ValueError("exercise mode needs the exercise sketch image (zones)")
    import hashlib
    import shutil

    sketch_key = file_key(paths.sketch, content=True) + "_" + hashlib.sha1("|".join(names_in(exercise_text)).encode()).hexdigest()[:6]
    zpath = cache / f"zones_{sketch_key}.geojson"
    gpath = cache / f"georef_{sketch_key}.json"
    mpath = cache / f"marks_{sketch_key}.png"
    if zpath.exists() and gpath.exists() and llm is None:
        zones_gdf = gpd.read_file(zpath)
        geo_facts = json.loads(gpath.read_text(encoding="utf-8"))
    else:
        geo = georeference_sketch(paths.sketch, labels)
        # marks found by code and georeferenced; named by the sketch reader (L9) when a model is given, else by position words
        zones_gdf, marks, mark_notes, named_by = read_sketch(paths.sketch, geo, exercise_text, llm=llm, out_png=str(mpath))
        zones_gdf.to_file(zpath, driver="GeoJSON")
        areas_km2 = dict(zip(zones_gdf["mark"], zones_gdf.to_crs(L93).area / 1e6))
        geo_facts = {"rotation": geo.rotation, "inliers": geo.inliers, "median_residual_m": round(geo.median_residual_m),
                     "matches": [(m[0], m[1]) for m in geo.matches], "named_by": named_by, "mark_notes": mark_notes,
                     "marks": [{"id": m.id, "colour": m.colour, "px": m.px, "area_km2": round(float(areas_km2[m.id]), 1), "name": m.name,
                                "label": m.label, "role": m.role, "named_by": m.named_by} for m in marks]}
        gpath.write_text(json.dumps(geo_facts, ensure_ascii=False, indent=1), encoding="utf-8")
    if mpath.exists():
        shutil.copyfile(mpath, out / "sketch_marks.png")
    missing = [z for z in (spec.origin_zone, spec.destination_zone) if z not in set(zones_gdf["zone"])]
    if missing:
        raise ValueError(f"zone(s) {missing} named in the order not found among the marks read on the sketch "
                         f"({list(zones_gdf['zone'])}); see {out / 'sketch_marks.png'} and the georeferencing facts")
    data_box = shapely.box(*rep_bd.bounds)
    zones_gdf["geometry"] = zones_gdf.geometry.intersection(data_box)
    zones = {z: g for z, g in zip(zones_gdf["zone"], zones_gdf.to_crs(L93).geometry)}
    run.facts["georef"] = {k: v for k, v in geo_facts.items() if k not in ("marks", "mark_notes")}
    run.facts["marks"] = geo_facts.get("marks", [])
    run.facts["mark_notes"] = geo_facts.get("mark_notes", [])
    run.facts["zones"] = {z: round(float(a), 1) for z, a in zip(zones_gdf["zone"], zones_gdf.to_crs(L93).area / 1e6)}
    t = run.tick("georeference sketch and read zones", t)

    # 5. annotate roads with built-up and town areas
    edges = list(G.edges)
    lines = edge_lines(G, edges)
    run.facts["annotate"] = annotate_areas(G, lines, edges, builtup, towns_big)
    t = run.tick("annotate roads (built-up, towns)", t)

    # 6. COA-Net mission context: order -> group -> per-road evaluation
    for z, poly in zones.items():
        c = gpd.GeoSeries([poly.centroid], crs=L93).to_crs("EPSG:4326").iloc[0]
        G.nodes[nearest_node_fast(G, (c.x, c.y))]["name"] = f"{z} zone"
    catalogue = load_catalogue()
    order = (f"ORDER EX-1 - exercise 1\nPhase: day\nIntent: {spec.k} separate itineraries for the PFM.\n"
             f"Convoy PFM: {spec.n_vehicles} x {spec.vehicle} from {spec.origin_zone} zone to {spec.destination_zone} zone, depart 06:00, priority 1.\n")
    from .realdata import systemic_unknowns

    reqs, relax_notes = systemic_unknowns(G, (requirements if requirements is not None else load_requirements()) + ex_reqs)
    run.facts["systemic_unknowns"] = relax_notes
    H = CorridorGraph(G, max_rank=max_rank)
    mission = parse_order(order, G, catalogue)
    ctx = build_context(H, mission, reqs, catalogue)
    grp = next(iter(ctx.groups.values()))
    evals = grp.coster.evals
    blocked: dict[str, int] = {}
    for e in evals.values():
        for rid, tier, detail in e.blocked[:1]:
            if rid == "X-02":
                key = f"{rid} inside a town > {spec.min_town_pop}"
            else:  # one row per rule and limit, not per measured value ("slope 15.4 % > 15 %" -> "slope > 15 %")
                key = f"{rid} " + re.sub(r"^(slope|width|clearance|bridge MLC|posted limit) [\d.]+ ?(%|m|t)? ?", r"\1 ",
                                         detail.split("(")[0].strip())
            key = key if len(key) < 70 else key[:70]
            blocked[key] = blocked.get(key, 0) + 1
    run.facts["mission"] = {"order": order, "active_requirements": sorted(r.id + " " + r.template for r in grp.active.reqs) if hasattr(grp.active, "reqs") else None,
                            "edges": len(evals), "edges_ok": sum(e.ok for e in evals.values()),
                            "blocked_by_first_reason": dict(sorted(blocked.items(), key=lambda kv: -kv[1])[:15])}
    t = run.tick("mission context and per-road evaluation", t)

    # 7. solve NET.DISTINCT
    R = RoadArrays.build(G, evals, lines)
    ok_nodes = np.zeros(len(R.nodes), bool)
    ok_nodes[R.u[R.ok]] = True
    ok_nodes[R.v[R.ok]] = True
    pts = shapely.points(R.node_xy)
    # itineraries start and end at least one georeferencing residual inside the zones, so that the
    # sketch's imprecision cannot put an end point outside the zone the ETLO drew
    margin = float(geo_facts.get("median_residual_m") or 0.0)
    inner = {}
    for z, poly in zones.items():
        shr = poly.buffer(-margin) if margin > 0 else poly
        inner[z] = shr if not shr.is_empty and shr.area >= 0.25 * poly.area else poly
    src = np.flatnonzero(ok_nodes & shapely.contains(inner[spec.origin_zone], pts))
    dst = np.flatnonzero(ok_nodes & shapely.contains(inner[spec.destination_zone], pts))
    run.facts["zone_nodes"] = {"origin": int(len(src)), "destination": int(len(dst)), "inner_margin_m": round(margin),
                               "inner_km2": {z: round(g.area / 1e6, 1) for z, g in inner.items()}}
    # T2/T3 preferences priced as in COA-Net: built-up and unknown-attribute roads cost more
    pen = np.ones(len(R.edges))
    for i, e in enumerate(R.edges):
        ev = evals[e]
        if ev.builtup:
            pen[i] *= 1.5
        if any("may be under" in detail for _, detail in ev.unknown):
            pen[i] *= 1.5  # possibly too narrow for the column: avoided like a built-up road
        elif ev.pref:
            pen[i] *= 1.2
        if G.edges[e].get("source_id") == "scan":
            # a link drawn across a label (1.5x), or a road the learned corrector doubts (up to 2x): used only if it pays
            pen[i] *= uncertainty_penalty(G.edges[e])
    t = run.tick("road arrays and zone nodes", t)
    variants = {}
    if compare or not turn_aware:
        variants["junction graph (no turn cost)"] = Router(R, src, dst, pen)
    if turn_aware:
        variants["turn-aware line graph"] = TurnRouter(R, src, dst, pen, turn_min=spec.turn_penalty_min, sharp_deg=spec.sharp_turn_deg,
                                                       sharp_min=spec.sharp_turn_min)
        t = run.tick("turn graph (line graph of the roads)", t)
    results = {}
    for name, router in variants.items():
        results[name] = solve_distinct(R, src, dst, spec.k, spec.min_sep_m, n_seeds=n_seeds, penalty=pen, router=router)
        t = run.tick(f"NET.DISTINCT solver, {name}", t)
    for name, solver in (extra_solvers or {}).items():
        results[name] = solver(R, src, dst, pen, spec)
        t = run.tick(f"NET.DISTINCT solver, {name}", t)
    chosen = "turn-aware line graph" if turn_aware else "junction graph (no turn cost)"
    res = results[chosen]
    probe = variants.get("turn-aware line graph") or TurnRouter(R, src, dst, pen)
    run.facts["solver"] = {"variant": chosen, "tried": res.tried, "feasible_sets": res.feasible_sets, "seconds": round(res.seconds, 1),
                           "log": res.log, "alternatives": res.alternatives}
    cmp = {}
    for name, r_ in results.items():
        crit = sharp = 0
        for it in r_.itineraries:
            crit += sum(1 for x in turns_on(it.line, spec.turn_radius_warn_m, spec.turn_radius_crit_m, half_chord_m=half_chord) if x["severity"] == "critical")
            sharp += int(np.nansum(probe.turns_of(it.edge_idx) > spec.sharp_turn_deg))
        cmp[name] = {"itineraries": len(r_.itineraries), "total_time_min": round(sum(i.time_min for i in r_.itineraries), 1),
                     "total_km": round(sum(i.length_m for i in r_.itineraries) / 1000, 1), "critical_turns": crit,
                     "junction_turns_over_sharp_deg": sharp, "seconds": round(r_.seconds, 1), "distinct_sets": r_.feasible_sets,
                     "total_cost_min": round(sum(float((R.time_min * pen)[i.edge_idx].sum()) for i in r_.itineraries), 1),
                     # the same cost plus the turn-aware router's junction turn costs: the objective of the turn-aware search
                     "total_cost_with_turns_min": round(sum(float((R.time_min * pen)[i.edge_idx].sum())
                                                            + float(np.nansum(probe.turn_cost_of(i.edge_idx))) for i in r_.itineraries), 1),
                     "min_separation_m": _min_sep(r_.itineraries), "log": r_.log}
    run.facts["router_comparison"] = cmp
    # 8. facts, turns, checker
    small = None
    if builtup is not None and towns:
        small_names = [t_ for t_ in towns if spec.min_town_pop is None or t_.population <= spec.min_town_pop]
        small = town_areas(builtup, small_names, 0)
        small = small.to_crs(L93) if len(small) else None
    for it in res.itineraries:
        it.facts = itinerary_facts(R, it, evals, small, spec, need_width_m=2 * catalogue[spec.vehicle].width_m + 0.5)
        it.facts["width_need_m"] = round(catalogue[spec.vehicle].width_m + (grp.active.param("SEG.GAUGE", "width_margin_m", 0.0) or 0.0), 2)
        it.turns = turns_on(it.line, spec.turn_radius_warn_m, spec.turn_radius_crit_m, half_chord_m=half_chord)
        mark_junctions(G, R, it)
        it.facts["turns_difficult"] = sum(1 for x in it.turns if x["severity"] == "difficult")
        it.facts["turns_critical"] = sum(1 for x in it.turns if x["severity"] == "critical")
        it.facts["min_radius_m"] = min((x["radius_m"] for x in it.turns), default=None)
    checks = check(R, res.itineraries, spec, zones, towns_big)
    run.facts["checks"] = checks
    t = run.tick("itinerary facts, turns and checker", t)

    # 9. sanity checks against OSM and the scan
    if paths.osm and osm_check:
        from .realdata import OSM_DRIVABLE, read_lines

        osm, _ = read_lines(paths.osm, "utf-8", columns=["fclass"])
        osm = osm[osm["fclass"].isin(OSM_DRIVABLE)]
        run.facts["osm_check"] = {"drivable_ways": int(len(osm))}
        otree = shapely.STRtree(osm.to_crs(L93).geometry.values)
        agree = {}
        for it in res.itineraries:
            samp = shapely.points(np_resample(it.line, 50.0))
            idx = otree.query(samp, predicate="dwithin", distance=25.0)
            hit = np.unique(idx[0]) if idx.size else np.array([], int)
            agree[it.id] = round(len(hit) / len(samp), 3)
        run.facts["osm_agreement_25m"] = agree
        t = run.tick("cross-check with OSM", t)
    if network != "bdtopo" and paths.bdtopo and osm_check and res.itineraries:
        # the network came from the image: does every itinerary run on roads that BD TOPO has?
        from .realdata import BDTOPO_EXCLUDED, read_lines

        bd, _ = read_lines(paths.bdtopo, "cp1252", columns=["NATURE"])
        bd = bd[~bd["NATURE"].str.lower().isin(BDTOPO_EXCLUDED)]
        btree = shapely.STRtree(bd.to_crs(L93).geometry.values)
        agree, agree50, worst = {}, {}, {}
        for it in res.itineraries:
            pts = np_resample(it.line, 50.0)
            samp = shapely.points(pts)
            hits = {}
            for dist in (25.0, 50.0):
                idx = btree.query(samp, predicate="dwithin", distance=dist)
                hits[dist] = np.zeros(len(samp), bool)
                if idx.size:
                    hits[dist][np.unique(idx[0])] = True
            agree[it.id] = round(float(hits[25.0].mean()), 3)
            agree50[it.id] = round(float(hits[50.0].mean()), 3)
            # longest stretch with no BD TOPO road within 50 m (the map displaces roads by up to a few tens of
            # metres): where the image may have invented a link
            run_, best = 0, 0
            for h in hits[50.0]:
                run_ = 0 if h else run_ + 1
                best = max(best, run_)
            worst[it.id] = int(best * 50)
        run.facts["bdtopo_agreement_25m"] = agree
        run.facts["bdtopo_agreement_50m"] = agree50
        run.facts["bdtopo_longest_unmatched_m"] = worst
        t = run.tick("cross-check with BD TOPO (sanity check of the image network)", t)
    if paths.scan100 and res.itineraries and network in ("bdtopo", "osm"):
        from shapely import affinity

        from .audit import scan_agreement

        # road-ink colours learnt from every classified road (ranks 1 to 4, motorway to tertiary): learnt from
        # trunk/primary/secondary only, they missed the dual-carriageway symbol (23 % of IT-1's motorway-class points)
        ref = R.lines[np.flatnonzero(np.array([G.edges[e].get("road_rank", 5) for e in R.edges]) <= 4)]
        lines_ = [it.line for it in res.itineraries]
        share = scan_agreement(paths.scan100, lines_, ref, tol_px=2)
        control = scan_agreement(paths.scan100, [affinity.translate(g, 300.0, 0.0) for g in lines_], ref, tol_px=2)
        run.facts["scan_agreement_2px"] = {it.id: v for it, v in zip(res.itineraries, share)}
        run.facts["scan_agreement_2px_shifted_300m"] = {it.id: v for it, v in zip(res.itineraries, control)}
        t = run.tick("cross-check with the 1:100k scan", t)

    # 10. difficult areas over the study area: convex hull of the two zones (the band the itineraries cross)
    study = shapely.union_all(list(zones.values())).convex_hull
    extra = [(f"critical turn on an itinerary (radius < {spec.turn_radius_crit_m:g} m)", shapely.Point(x["x"], x["y"]))
             for it in res.itineraries for x in it.turns if x["severity"] == "critical"]
    areas = difficult_areas(R, study, spec, towns_big, builtup, need_width_m=2 * catalogue[spec.vehicle].width_m + 0.5,
                            single_lane_m=catalogue[spec.vehicle].width_m + 1.0, extra=extra)
    run.facts["difficult_areas"] = {k: {"features": int(n), "area_km2": float(a)} for k, n, a in zip(areas["kind"], areas["features"], areas["area_km2"])}
    run.facts["study_area_km2"] = round(study.area / 1e6, 1)
    t = run.tick("difficult-manoeuvre areas (study area)", t)

    # 11. outputs (every model call is done by now: the facts say which model ran where, for the decision text too)
    if llm is not None:
        u = llm.usage
        run.facts["llm"] = {**(llm.describe() if hasattr(llm, "describe") else {"model": type(llm).__name__}),
                            "calls": u.calls, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens, "by_stage": u.by_stage}
    write_outputs(run, res, areas, zones_gdf, towns_big, spec, paths, checks)
    run.tick("outputs", t)
    run.timings["total"] = round(time.perf_counter() - T, 1)
    (out / "metrics.json").write_text(json.dumps({"timings_s": run.timings, "facts": run.facts}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return run


def _min_sep(its) -> float | None:
    """Smallest distance (m) between two itineraries of a set."""
    d = [its[i].line.distance(its[j].line) for i in range(len(its)) for j in range(i)]
    return round(min(d)) if d else None


def mark_junctions(G, R, it, tol_m: float = 25.0) -> None:
    """Flag each turn that sits on a junction of the route (a node with three or more neighbouring roads)."""
    idx = [R.node_index[n] for n in it.nodes]
    deg = np.array([len(set(G.predecessors(n)) | set(G.successors(n))) for n in it.nodes])
    xy = R.node_xy[idx][deg >= 3]
    for tr in it.turns:
        d = np.hypot(xy[:, 0] - tr["x"], xy[:, 1] - tr["y"]).min() if len(xy) else np.inf
        tr["at_junction"] = bool(d <= tol_m)
    it.facts["turns_at_junctions"] = sum(1 for x in it.turns if x["at_junction"])


def nearest_node_fast(G, xy):
    import numpy as np

    nodes = list(G.nodes)
    a = np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in nodes])
    d = (a[:, 0] - xy[0]) ** 2 * np.cos(np.radians(xy[1])) ** 2 + (a[:, 1] - xy[1]) ** 2
    return nodes[int(np.argmin(d))]


def np_resample(line, step):
    from .exercise import resample

    return resample(line, step)


def write_outputs(run: ScenarioRun, res, areas, zones_gdf, towns_big, spec, paths: ScenarioPaths, checks) -> None:
    import geopandas as gpd
    import shapely

    out = run.out
    feats = []
    for it in res.itineraries:
        g = gpd.GeoSeries([it.line], crs=L93).to_crs("EPSG:4326").iloc[0]
        feats.append({"type": "Feature", "geometry": shapely.geometry.mapping(g),
                      "properties": {"kind": "itinerary", "id": it.id, **{k: v for k, v in it.facts.items() if k != "unknown_risks"}}})
        for j, tr in enumerate(it.turns, start=1):
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [tr["lon"], tr["lat"]]},
                          "properties": {"kind": "difficult_turn", "itinerary": it.id, "turn": f"{it.id}-T{j}",
                                         "curvature_radius_m": tr["radius_m"], "deflection_deg": tr["deflection_deg"],
                                         "severity": tr["severity"], "at_junction": tr.get("at_junction"),
                                         "km_from_start": tr["km"]}})
    (out / "itineraries.geojson").write_text(json.dumps({"type": "FeatureCollection", "name": "itineraries",
                                                         "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
                                                         "features": feats}, ensure_ascii=False), encoding="utf-8")
    areas.to_file(out / "difficult_areas.geojson", driver="GeoJSON")
    zones_gdf.to_file(out / "zones.geojson", driver="GeoJSON")
    if towns_big is not None and len(towns_big):
        towns_big.to_file(out / "towns_over_threshold.geojson", driver="GeoJSON")
    if paths.scan250 or paths.scan100:
        render_overlay(paths.scan250 or paths.scan100, out / "map_itineraries.png", res, zones_gdf, towns_big, None, "Itineraries")
        render_overlay(paths.scan250 or paths.scan100, out / "map_difficult_areas.png", res, zones_gdf, towns_big, areas,
                       "Areas where manoeuvring is difficult")
    (out / "decision_process.txt").write_text(decision_text(run, res, spec, checks), encoding="utf-8")


COLORS = ["#d62728", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e"]


def render_overlay(tif: str, path: Path, res, zones_gdf, towns_big, areas, title: str) -> None:
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from rasterio.enums import Resampling

    geoms = [gpd.GeoSeries([it.line], crs=L93).to_crs("EPSG:4326").iloc[0] for it in res.itineraries]
    b = zones_gdf.total_bounds
    for g in geoms:
        gb = g.bounds
        b = [min(b[0], gb[0]), min(b[1], gb[1]), max(b[2], gb[2]), max(b[3], gb[3])]
    pad = 0.04
    W, S, E, N = b[0] - pad, b[1] - pad, b[2] + pad, b[3] + pad
    with rasterio.open(tif) as src:
        W, S, E, N = max(W, src.bounds.left), max(S, src.bounds.bottom), min(E, src.bounds.right), min(N, src.bounds.top)
        win = rasterio.windows.from_bounds(W, S, E, N, src.transform)
        f = max(1, int(win.width // 2400))
        a = src.read(1, window=win, out_shape=(int(win.height // f), int(win.width // f)), resampling=Resampling.nearest)
        cm = src.colormap(1)
        lut = np.zeros((256, 3), np.uint8)
        for k, v in cm.items():
            lut[k] = v[:3]
    lat0 = (S + N) / 2
    fig, ax = plt.subplots(figsize=(11, 11 * (N - S) / ((E - W) * np.cos(np.radians(lat0)))))
    ax.imshow(lut[a], extent=(W, E, S, N), alpha=0.75)
    ax.set_aspect(1 / np.cos(np.radians(lat0)))
    zones_gdf.plot(ax=ax, color="#3b6fd8", alpha=0.18, edgecolor="#2346a0", linewidth=1.2)
    for z, g in zip(zones_gdf["zone"], zones_gdf.geometry):
        c = g.representative_point()
        ax.annotate(f"{z} zone", (c.x, c.y), color="#2346a0", fontsize=10, weight="bold", ha="center")
    if towns_big is not None and len(towns_big):
        towns_big.plot(ax=ax, facecolor="none", edgecolor="#b00000", hatch="///", linewidth=1.0)
        for nm, g in zip(towns_big["town"], towns_big.geometry):
            c = g.representative_point()
            ax.annotate(nm.split(" / ")[0].title(), (c.x, c.y), color="#b00000", fontsize=7)
    if areas is not None and len(areas):
        cmap = plt.get_cmap("tab10")
        markers = {"bend": "^", "bridge": "s", "tunnel": "D", "ford": "v", "slope": "P", "carriageway": "_", "critical turn": "x"}
        for i, (k, g) in enumerate(zip(areas["kind"], areas.geometry)):
            mk = next((m for key, m in markers.items() if key in k), None)
            if mk is None:  # areal layers (built-up, prohibited towns)
                gpd.GeoSeries([g], crs="EPSG:4326").plot(ax=ax, color=cmap(i % 10), alpha=0.55)
                ax.scatter([], [], marker="s", color=cmap(i % 10), alpha=0.55, label=k)
            else:  # small features: one marker per part, so they stay visible at theatre scale
                parts = gpd.GeoSeries([g], crs="EPSG:4326").explode(index_parts=False).representative_point()
                ax.scatter(parts.x, parts.y, marker=mk, s=10 if mk != "x" else 22, color=cmap(i % 10), alpha=0.85,
                           linewidths=1.0, label=f"{k} ({len(parts)} places)", zorder=4)  # overlapping features merge
    for it, g, col in zip(res.itineraries, geoms, COLORS):
        x, y = g.xy if g.geom_type == "LineString" else (np.concatenate([np.array(l.xy[0]) for l in g.geoms]),
                                                          np.concatenate([np.array(l.xy[1]) for l in g.geoms]))
        ax.plot(x, y, color=col, lw=2.6 if areas is None else 1.6, label=f"{it.id}: {it.facts.get('length_km')} km, {it.facts.get('time_min')} min")
        if areas is None:
            tx = [t for t in it.turns]
            ax.scatter([t["lon"] for t in tx if t["severity"] == "critical"], [t["lat"] for t in tx if t["severity"] == "critical"],
                       marker="x", color=col, s=28, zorder=5)
    ax.set_xlim(W, E)
    ax.set_ylim(S, N)
    ax.set_title(title + " (background: IGN 1:250 000 scan; crosses: critical turns)" if areas is None else title)
    ax.legend(loc="lower left", fontsize=7, framealpha=0.85)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def decision_text(run: ScenarioRun, res, spec, checks) -> str:
    F = run.facts
    L = []
    L.append("COA-Net - exercise 1 - decision process")
    L.append("=" * 40)
    L.append("")
    L.append("1. What was asked (compiled from the exercise text)")
    for what, q in F["exercise"]["recognised"]:
        L.append(f"   - {what}: \"{q}\"")
    L.append("   Requirement instances added to the seed doctrine: X-01 NET.DISTINCT "
             f"(k={spec.k}, {spec.min_sep_m / 1000:g} km, no intersection), X-02 SEG.TOWN (> {spec.min_town_pop} inhabitants).")
    L.append("")
    L.append("2. Data used and what was read from it")
    if "load_scan" in F:
        sc = F["load_scan"]
        L.append(f"   - Road graph: read from the image ({F['network']['source']}), not from a shapefile: road symbols classified by "
                 f"colour (blue expressway, red main road, orange departmental road, white road, dashed track), centre lines thinned "
                 f"and joined at junctions and across labels; {sc['nodes']} junctions, {sc['km']:.0f} km of road in the largest connected "
                 f"network ({sc['largest_scc_share']:.0%} of the junctions). The map gives no width, load class or slope: they stay "
                 f"unknown. White roads are one symbol for minor and secondary roads, so roads of class 5 and better are searched.")
        L.append("   - BD TOPO and OSM are used only to check the result (section 5).")
    elif "load_osm" in F:
        om = F["load_osm"]
        L.append(f"   - Road graph: OSM shapefile (Geofabrik), {om['kept']} of {om['features']} ways kept (drivable classes), {om['nodes']} "
                 f"junctions, {om['km']:.0f} km of road. Lon/lat EPSG:4326 assumed, lengths and distances computed in Lambert-93.")
        L.append("   - BD TOPO is used only to check the result (section 5).")
    else:
        bd = F["load_bdtopo"]
        L.append(f"   - Road graph: IGN BD TOPO v2 shapefile, {bd['kept']} of {bd['features']} sections kept (footpaths, steps, cycle tracks "
                 f"and roads under construction removed), {bd['nodes']} junctions, {bd['km']:.0f} km of road. No .prj: lon/lat EPSG:4326 assumed "
                 "from the bounds, lengths and distances computed in Lambert-93.")
    if "towns" in F:
        big = ", ".join(f"{n.title()} ({p})" for n, p in F["towns_big"])
        L.append(f"   - Towns and populations read by OCR on the IGN 1:100 000 scan (population in thousands printed next to each town "
                 f"name): {len(F['towns'])} towns read; above {spec.min_town_pop} (names as read): {big}. Town areas = built-up tint "
                 f"of the scan ({F['builtup']['polygons']} built-up polygons, {F['builtup']['area_km2']} km2); towns whose built-up "
                 "areas touch are one area.")
    g = F["georef"]
    L.append(f"   - Zones: the exercise sketch was georeferenced by matching {g['inliers']} place names read on the sketch with the same names "
             f"on the 1:100 000 scan (median residual {g['median_residual_m']} m); the two blue areas were vectorised. "
             + ", ".join(f"{z} zone {a} km2" for z, a in F["zones"].items()) + " (clipped to the data extent).")
    named = {"model": "the language model (L9), its answer checked against the order",
             "position words": "the order's position words"}.get(g.get("named_by", ""), "")
    lm = F.get("llm")
    if lm:
        where = "local" if lm.get("local", True) else "remote"
        L.append(f"   - Language model: {lm.get('model', '?')} served at {lm.get('base_url', '?')} ({where}), {lm.get('calls', 0)} calls"
                 + (f"; the server stopped answering ({lm['down']}), so the later roles used their deterministic fallbacks"
                    if lm.get("down") else "") + (f". The zones were named by {named}." if named else "."))
    elif named:
        L.append(f"   - Language model: none. The zones were named by {named}.")
    zn = F.get("zone_nodes", {})
    if zn.get("inner_margin_m"):
        L.append(f"   - Itineraries start and end at least {zn['inner_margin_m']} m (the georeferencing residual) inside the zones: "
                 + ", ".join(f"{z} {a} km2" for z, a in zn["inner_km2"].items())
                 + f"; {zn['origin']} usable junctions in the origin zone and {zn['destination']} in the destination zone.")
    L.append("")
    L.append("3. Which roads the column may use (COA-Net requirement checks on every road)")
    m = F["mission"]
    L.append(f"   - Column: {spec.n_vehicles} x pfm_carrier (assumed 32 t, 3.0 m wide, 3.9 m high, 12 m long; to confirm).")
    L.append(f"   - {m['edges_ok']} of {m['edges']} directed road sections allowed. First blocking reason, by count:")
    for k, v in m["blocked_by_first_reason"].items():
        L.append(f"       {v:7d}  {k}")
    for n in F.get("systemic_unknowns", []):
        L.append("   - Data-driven policy change: " + n)
    if "load_scan" in F:
        L.append("   - Width, slope and load class are not on the map: every road keeps them unknown, which does not block but is "
                 "priced as risk, as in COA-Net; the map symbol gives the road class (and so the speed) and the bridges (road over a "
                 "river). Links the reader drew across a label cost 1.5x, so a route uses them only when it pays.")
    else:
        L.append("   - Other unknown attributes (width where BD TOPO gives 0, slope where the altitude precision is unknown) do not block: "
                 "they are priced as risk, as in COA-Net.")
        L.append("   - Widths: BD TOPO gives LARGEUR in whole or half metres (a 3 m road may be up to 3.5 m wide); a road is excluded only if "
                 "it is too narrow even at the top of that rounding; if it may be too narrow, it stays usable, is priced as a risk and "
                 "its length is reported per itinerary.")
        L.append("   - Slope is computed from the end altitudes of each BD TOPO section (Z_INI, Z_FIN) and their stated precision (PREC_ALTI, "
                 "1 m or 2.5 m): a section is excluded only if it is steeper than the column limit even at the low end of that interval; "
                 "if only the high end exceeds the limit, the section stays usable and is priced as a risk.")
    L.append("")
    L.append("4. How the three itineraries were chosen")
    L.append("   - Travel time per road from the road class and the march speed cap; built-up roads and roads that may be too narrow "
             "for the column cost 1.5x, roads with another unknown or a soft (T2/T3) issue cost 1.2x.")
    L.append("   - Seeds: diverse fastest routes through via points on a 3 km lattice (within 1.6x the fastest). For each seed, "
             f"every next itinerary is the fastest one outside a {spec.min_sep_m / 1000:g} km corridor around the itineraries already chosen "
             "(start and end anywhere in the zones). Among all complete sets the one with the smallest total time is kept.")
    s = F["solver"]
    for line in s["log"]:
        L.append(f"   - {line}")
    if s["alternatives"]:
        L.append("   - Generalised cost of the chosen set and of the next best distinct sets: "
                 + "; ".join(f"{t:.0f}" for t, _ in s["alternatives"]))
    if F.get("router_comparison"):
        L.append("   - Junction turns: the search runs on the line graph of the roads (one state per road and direction, one arc per "
                 "allowed turn), so each turn is priced; compared with the same search without turn costs:")
        for name, c in F["router_comparison"].items():
            L.append(f"       {name}: {c['itineraries']} itineraries, {c['total_km']} km, {c['total_time_min']} min in total, "
                     f"{c['critical_turns']} critical turns ({c['junction_turns_over_sharp_deg']} junction turns over the sharp threshold), "
                     f"{c['distinct_sets']} distinct feasible sets, {c['seconds']} s")
    L.append("")
    L.append("5. Result")
    for it in res.itineraries:
        f = it.facts
        if f["roads"]:
            L.append(f"   {it.id}: {f['length_km']} km, {f['time_min']} min; roads {' - '.join(f['roads'][:12])}")
        else:  # image network: no road numbers, the map symbols instead
            L.append(f"   {it.id}: {f['length_km']} km, {f['time_min']} min; map symbols "
                     + ", ".join(f"{k} {v} km" for k, v in f.get("map_symbols_km", {}).items()))
        L.append(f"      bridges {f['bridges']} (load class unknown), tunnels {f['tunnels']}, fords {f['fords']}; built-up {f['builtup_km']} km"
                 + (f" ({', '.join(t.title().rstrip('-') for t in f['built_up_towns_crossed'])}, all under the threshold)"
                    if f["built_up_towns_crossed"] else ""))
        L.append(f"      turns: {f['turns_critical']} critical (radius < {spec.turn_radius_crit_m:g} m) and {f['turns_difficult']} difficult "
                 f"(< {spec.turn_radius_warn_m:g} m), {f.get('turns_at_junctions', 0)} of them at junctions; smallest radius "
                 f"{f['min_radius_m']} m; width unknown on {f['width_unknown_km']} km, possibly under {f.get('width_need_m', 0):g} m on "
                 f"{f.get('width_uncertain_km', 0)} km; "
                 + (f"steepest section {f['max_slope_pct']} %" if f.get("slope_known", True) else "slope unknown (not on the map)"))
        checks_ = []
        if "bdtopo_agreement_25m" in F:
            checks_.append(f"{F['bdtopo_agreement_25m'][it.id]:.1%} of the route lies within 25 m of a BD TOPO road and "
                           f"{F['bdtopo_agreement_50m'][it.id]:.1%} within 50 m (the map displaces roads; longest stretch with no BD TOPO "
                           f"road within 50 m: {F['bdtopo_longest_unmatched_m'][it.id]} m)")
        if "osm_agreement_25m" in F:
            checks_.append(f"{F['osm_agreement_25m'][it.id]:.1%} of the route lies within 25 m of an OSM drivable road")
        if "scan_agreement_2px" in F:
            checks_.append(f"{F['scan_agreement_2px'][it.id]:.1%} of its points fall on road ink of the 1:100 000 scan within 2 pixels "
                           f"(the same line moved 300 m east: {F['scan_agreement_2px_shifted_300m'][it.id]:.1%})")
        if checks_:
            L.append("      sanity checks: " + "; ".join(checks_))
    L.append("")
    L.append("6. Checks (recomputed from the geometry)")
    for name, ok, detail in checks:
        L.append(f"   [{'OK' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    L.append("")
    L.append("7. Assumptions and open points")
    for a in F["exercise"]["assumptions"]:
        L.append(f"   - {a}")
    if F.get("towns_unresolved"):
        L.append("   - Upper-case labels with no population figure read (rivers, forests, fragments, or towns to check by eye): "
                 + ", ".join(F["towns_unresolved"][:20]))
    L.append("   - Bridge load classes are not in BD TOPO or OSM: every bridge on the itineraries is a reconnaissance task before the move.")
    L.append("")
    L.append("Timings (s): " + ", ".join(f"{k} {v}" for k, v in run.timings.items()))
    return "\n".join(L) + "\n"

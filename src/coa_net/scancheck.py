"""Sanity checks of an image-derived road network against the shapefiles (BD TOPO, OSM).

The brief: "focusing on the image input and keeping the shapefiles for sanity checks is encouraged". Nothing
here feeds the plan; it measures the network read by :mod:`coa_net.scanroads` with the usual road-extraction
measures, so that the jury (and the ETLO) can see how far the image alone can be trusted:

* **completeness** - share of the reference length that has an extracted road within ``buffer_m``
  (per BD TOPO class: the 1:100 000 does not draw every lane), **correctness** - share of the extracted
  length that has a reference road within ``buffer_m``, **quality** (Wiedemann 1998);
* **registration** - median and mean offset vector between matched points (is the scan where the vectors are?);
* **class agreement** - map symbol against BD TOPO importance, length-weighted;
* **junctions** - precision and recall of junctions (three or more roads) within ``junction_m``;
* **routes** (APLS-like) - shortest-path length between the same pairs of points on both networks;
* **bridges** - scan bridges near a BD TOPO bridge.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import networkx as nx
import numpy as np

from .realdata import L93

log = logging.getLogger(__name__)

BD_CLASS_ORDER = ["1 main", "2", "3", "4", "5 minor", "track", "other"]


def _bd_class(nature: str, importance: str) -> str:
    nl = (nature or "").lower()
    if nl == "chemin" or "empierr" in nl:
        return "track"
    imp = str(importance or "").strip()
    return {"1": "1 main", "2": "2", "3": "3", "4": "4", "5": "5 minor"}.get(imp, "other")


def reference_lines(bdtopo: str, osm: str | None = None):
    """BD TOPO roads (classes for the report) and OSM drivable roads, in Lambert-93."""
    from .realdata import BDTOPO_EXCLUDED, OSM_DRIVABLE, read_lines

    bd, _ = read_lines(bdtopo, "cp1252", ["NATURE", "IMPORTANCE", "POS_SOL", "FRANCHISST", "ETAT"])
    bd = bd[~bd["NATURE"].str.lower().isin(BDTOPO_EXCLUDED)].copy()
    bd["cls"] = [_bd_class(n, i) for n, i in zip(bd["NATURE"], bd["IMPORTANCE"])]
    bd["bridge"] = (bd["POS_SOL"].fillna(0).astype(int) > 0) | (bd["FRANCHISST"] == "Pont")
    bd = bd.to_crs(L93)
    om = None
    if osm:
        om, _ = read_lines(osm, "utf-8", ["fclass"])
        om = om[om["fclass"].isin(OSM_DRIVABLE)].to_crs(L93)
    return bd, om


def _points(geoms, step_m: float, labels=None):
    """Points every ``step_m`` along lines (L93), with the index of their line."""
    import shapely

    g = shapely.segmentize(np.asarray(geoms), step_m)
    xy, idx = shapely.get_coordinates(g, return_index=True)
    return xy, idx


def _seg_lengths(xy: np.ndarray, idx: np.ndarray) -> np.ndarray:
    """Length carried by each point: half of each adjacent segment of the same line."""
    w = np.zeros(len(xy))
    if len(xy) < 2:
        return w
    d = np.hypot(*np.diff(xy, axis=0).T)
    same = idx[1:] == idx[:-1]
    d = np.where(same, d, 0.0)
    w[:-1] += d / 2
    w[1:] += d / 2
    return w


def buffer_measures(test_xy, test_w, ref_xy, buffers=(15.0, 25.0, 40.0)) -> dict:
    """Share of the test length within each buffer of the reference points (densified at <= 10 m)."""
    from scipy.spatial import cKDTree

    d, j = cKDTree(ref_xy).query(test_xy, distance_upper_bound=max(buffers) + 10)
    out = {f"{b:g}m": float(test_w[d <= b].sum() / test_w.sum()) if test_w.sum() else 0.0 for b in buffers}
    return out, d, j


def graph_lines(G: nx.DiGraph):
    """One L93 line per undirected road of a canonical graph, with its edge data."""
    import geopandas as gpd
    import shapely

    seen, geoms, rows = set(), [], []
    for u, v, d in G.edges(data=True):
        k = (u, v) if str(u) <= str(v) else (v, u)
        if k in seen:
            continue
        seen.add(k)
        c = d.get("coords") or ((G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"]))
        geoms.append(shapely.LineString(c))
        rows.append({"u": u, "v": v, "highway": d.get("highway"), "rank": d.get("road_rank", 5), "symbol": d.get("scan_symbol"),
                     "bridge": bool(d.get("bridge")), "gap": bool(d.get("scan_gap"))})
    return gpd.GeoDataFrame(rows, geometry=geoms, crs=G.graph.get("crs", "EPSG:4326")).to_crs(L93)


def _csr(G: nx.DiGraph, keep_edge=None):
    from scipy.sparse import csr_matrix

    nodes = list(G.nodes)
    ix = {n: i for i, n in enumerate(nodes)}
    r, c, w = [], [], []
    for u, v, d in G.edges(data=True):
        if keep_edge is None or keep_edge(d):
            r.append(ix[u]), c.append(ix[v]), w.append(d["length_m"])
    M = csr_matrix((w, (r, c)), shape=(len(nodes), len(nodes)))
    return M, nodes, ix


def _node_xy_l93(G: nx.DiGraph, nodes) -> np.ndarray:
    from .realdata import to_l93

    return to_l93(np.array([[G.nodes[n]["x"], G.nodes[n]["y"]] for n in nodes], float))


def route_measures(G_ref: nx.DiGraph, G_test: nx.DiGraph, n_pairs: int = 300, max_rank: int = 5, snap_m: float = 60.0,
                   dmin_m: float = 5_000, dmax_m: float = 40_000, seed: int = 0, ref_max_rank: int | None = None) -> dict:
    """APLS-like: shortest-path length between the same pairs of points on both networks (test roads of rank
    <= ``max_rank``, reference roads of rank <= ``ref_max_rank``, tracks excluded). Pairs are drawn among
    reference junctions that have a test node within ``snap_m``; a pair with no test route scores 0."""
    from scipy.sparse.csgraph import dijkstra
    from scipy.spatial import cKDTree

    rr = max_rank if ref_max_rank is None else ref_max_rank
    Mr, nr, _ = _csr(G_ref, lambda d: d.get("road_rank", 5) <= rr)
    Mt, nt, _ = _csr(G_test, lambda d: d.get("road_rank", 5) <= max_rank)
    xr, xt = _node_xy_l93(G_ref, nr), _node_xy_l93(G_test, nt)
    deg = np.asarray((Mr > 0).sum(axis=1)).ravel() + np.asarray((Mr > 0).sum(axis=0)).ravel()
    cand = np.flatnonzero(deg >= 3)
    dt, jt = cKDTree(xt).query(xr[cand], distance_upper_bound=snap_m)
    snapped = np.isfinite(dt)
    cand_ok, jt_ok = cand[snapped], jt[snapped]
    rng = np.random.default_rng(seed)
    pairs = []
    tries = 0
    while len(pairs) < n_pairs and tries < n_pairs * 50 and len(cand_ok) > 2:
        tries += 1
        a, b = rng.integers(0, len(cand_ok), 2)
        d = np.hypot(*(xr[cand_ok[a]] - xr[cand_ok[b]]))
        if dmin_m <= d <= dmax_m:
            pairs.append((a, b))
    if not pairs:
        return {"pairs": 0}
    src = sorted({a for a, _ in pairs})
    Dr = dijkstra(Mr, directed=True, indices=[cand_ok[a] for a in src])
    Dt = dijkstra(Mt, directed=True, indices=[jt_ok[a] for a in src])
    row = {a: i for i, a in enumerate(src)}
    ratios, scores, missing_t, missing_r = [], [], 0, 0
    for a, b in pairs:
        lr, lt = Dr[row[a], cand_ok[b]], Dt[row[a], jt_ok[b]]
        if not np.isfinite(lr):
            missing_r += 1
            continue
        if not np.isfinite(lt):
            missing_t += 1
            scores.append(0.0)
            continue
        ratios.append(lt / lr)
        scores.append(1 - min(1.0, abs(lt - lr) / lr))
    ratios = np.asarray(ratios)
    return {
        "pairs": len(pairs), "reference_unreachable": missing_r, "test_unreachable": missing_t,
        "reference_junctions_with_test_node": f"{snapped.mean():.1%}",
        "apls_like": round(float(np.mean(scores)), 3) if scores else None,
        "length_ratio_median": round(float(np.median(ratios)), 3) if len(ratios) else None,
        "within_5pct": round(float(np.mean(np.abs(ratios - 1) <= 0.05)), 3) if len(ratios) else None,
        "within_10pct": round(float(np.mean(np.abs(ratios - 1) <= 0.10)), 3) if len(ratios) else None,
        "max_rank": max_rank, "ref_max_rank": rr, "snap_m": snap_m, "distance_km": [dmin_m / 1000, dmax_m / 1000],
    }


def _junctions(G: nx.DiGraph, max_rank: int) -> np.ndarray:
    nb: dict = {}
    for u, v, d in G.edges(data=True):
        if d.get("road_rank", 5) <= max_rank:
            nb.setdefault(u, set()).add(v)
            nb.setdefault(v, set()).add(u)
    ns = [n for n, s in nb.items() if len(s) >= 3]
    return _node_xy_l93(G, ns) if ns else np.zeros((0, 2))


def junction_measures(G_ref: nx.DiGraph, G_test: nx.DiGraph, tols=(30.0, 60.0)) -> dict:
    """Precision: scan junctions (roads, tracks excluded) near any BD TOPO junction (any road or track).
    Recall: BD TOPO junctions of importance 1-4 roads (all drawn on the map) near a scan junction.
    The map's roads are displaced by cartographic generalisation (symbols wider than the roads), so the
    offsets are larger than the registration error."""
    from scipy.spatial import cKDTree

    jt = _junctions(G_test, 5)
    j_any, j_main = _junctions(G_ref, 6), _junctions(G_ref, 4)
    if not len(jt) or not len(j_any) or not len(j_main):
        return {"test": len(jt)}
    dt, _ = cKDTree(j_any).query(jt)
    dr, _ = cKDTree(jt).query(j_main)
    out = {"test": len(jt), "reference_any": len(j_any), "reference_main": len(j_main),
           "median_offset_m": round(float(np.median(dr)), 1)}
    for t in tols:
        out[f"precision_{t:g}m"] = round(float(np.mean(dt <= t)), 3)
        out[f"recall_main_{t:g}m"] = round(float(np.mean(dr <= t)), 3)
    return out


def validate(G_scan: nx.DiGraph, bdtopo: str, osm: str | None = None, G_ref: nx.DiGraph | None = None, out: str | Path | None = None,
             step_m: float = 10.0, buffer_m: float = 25.0, n_pairs: int = 300) -> dict:
    """All sanity checks of an image-derived graph. Returns a dict and writes ``validation.md`` / ``.json`` in ``out``."""
    T = {}
    t = time.perf_counter()
    bd, om = reference_lines(bdtopo, osm)
    T["read shapefiles"] = time.perf_counter() - t
    t = time.perf_counter()
    sc = graph_lines(G_scan)
    sxy, sidx = _points(sc.geometry.values, step_m)
    sw = _seg_lengths(sxy, sidx)
    bxy, bidx = _points(bd.geometry.values, step_m)
    bw = _seg_lengths(bxy, bidx)
    # scan box only (both networks cover the same sheet; this keeps edge effects out)
    res: dict = {"buffer_m": buffer_m, "step_m": step_m, "scan_km": round(float(sw.sum()) / 1000), "bdtopo_km": round(float(bw.sum()) / 1000)}
    corr, d_sb, j_sb = buffer_measures(sxy, sw, bxy)
    comp, d_bs, j_bs = buffer_measures(bxy, bw, sxy)
    res["correctness_vs_bdtopo"] = {k: round(v, 3) for k, v in corr.items()}
    res["completeness_vs_bdtopo_all"] = {k: round(v, 3) for k, v in comp.items()}
    bcls = bd["cls"].values[bidx]
    per = {}
    for c in BD_CLASS_ORDER:
        m = bcls == c
        if m.any():
            per[c] = {"km": round(float(bw[m].sum()) / 1000), "completeness": round(float(bw[m & (d_bs <= buffer_m)].sum() / bw[m].sum()), 3)}
    res["completeness_by_bdtopo_class"] = per
    # correctness by scan symbol
    ssym = sc["symbol"].fillna("?").values[sidx]
    res["correctness_by_symbol"] = {s: {"km": round(float(sw[ssym == s].sum()) / 1000),
                                        "correctness": round(float(sw[(ssym == s) & (d_sb <= buffer_m)].sum() / sw[ssym == s].sum()), 3)}
                                    for s in ("blue", "red", "orange", "white", "dashed") if (ssym == s).any()}
    # quality (Wiedemann): TP_ref / (len_ref + FP_scan), on the classes the map draws (importance 1-4 + 5 + tracks)
    tp_ref = float(bw[d_bs <= buffer_m].sum())
    fp_scan = float(sw[d_sb > buffer_m].sum())
    res["quality_all"] = round(tp_ref / (float(bw.sum()) + fp_scan), 3)
    main = np.isin(bcls, ["1 main", "2", "3", "4"])
    sm = np.isin(ssym, ["blue", "red", "orange", "white"])
    res["quality_main_roads"] = round(float(bw[main & (d_bs <= buffer_m)].sum()) / (float(bw[main].sum()) + float(sw[sm & (d_sb > buffer_m)].sum())), 3)
    # registration
    m = d_sb <= buffer_m
    vec = bxy[j_sb[m]] - sxy[m]
    res["registration"] = {"median_distance_m": round(float(np.median(d_sb[m])), 1), "p90_distance_m": round(float(np.percentile(d_sb[m], 90)), 1),
                           "mean_offset_east_m": round(float(vec[:, 0].mean()), 1), "mean_offset_north_m": round(float(vec[:, 1].mean()), 1)}
    # class agreement (length-weighted) for matched scan points
    conf: dict = {}
    for s in ("blue", "red", "orange", "white", "dashed"):
        mm = m & (ssym == s)
        if not mm.any():
            continue
        cl = bcls[j_sb[mm]]
        w = sw[mm]
        conf[s] = {c: round(float(w[cl == c].sum() / w.sum()), 3) for c in BD_CLASS_ORDER if (cl == c).any()}
    res["symbol_vs_bdtopo_class"] = conf
    # bridges
    br = sc[sc["bridge"]]
    if len(br):
        import shapely

        mids = shapely.line_interpolate_point(br.geometry.values, 0.5, normalized=True)
        bdb = bd[bd["bridge"]]
        tree = shapely.STRtree(bdb.geometry.values)
        near = tree.query(mids, predicate="dwithin", distance=50.0)
        hit = np.zeros(len(mids), bool)
        if near.size:
            hit[np.unique(near[0])] = True
        res["bridges"] = {"scan_bridges": int(len(br)), "near_bdtopo_bridge_50m": round(float(hit.mean()), 3),
                          "bdtopo_bridge_sections": int(len(bdb))}
    # gaps joined across labels: are they on real roads?
    gp = ssym  # noqa: F841
    gaps = sc["gap"].values[sidx]
    if gaps.any():
        res["label_gap_joins"] = {"km": round(float(sw[gaps].sum()) / 1000, 1),
                                  "correctness": round(float(sw[gaps & (d_sb <= buffer_m)].sum() / sw[gaps].sum()), 3)}
    T["buffer measures"] = time.perf_counter() - t
    if om is not None:
        t = time.perf_counter()
        oxy, oidx = _points(om.geometry.values, step_m)
        ow = _seg_lengths(oxy, oidx)
        c2, _, _ = buffer_measures(sxy, sw, oxy)
        k2, _, _ = buffer_measures(oxy, ow, sxy)
        res["correctness_vs_osm"] = {k: round(v, 3) for k, v in c2.items()}
        res["completeness_vs_osm_all"] = {k: round(v, 3) for k, v in k2.items()}
        # union reference: a scan road is "real" if either shapefile has it
        from scipy.spatial import cKDTree

        d_os, _ = cKDTree(oxy).query(sxy, distance_upper_bound=buffer_m + 1)
        res["correctness_vs_either"] = round(float(sw[(d_sb <= buffer_m) | (d_os <= buffer_m)].sum() / sw.sum()), 3)
        T["OSM measures"] = time.perf_counter() - t
    if G_ref is not None:
        t = time.perf_counter()
        res["junctions"] = junction_measures(G_ref, G_scan)
        res["routes"] = route_measures(G_ref, G_scan, n_pairs=n_pairs)
        # the 1:100 000 leaves out about half of the minor lanes: also against BD TOPO importance 1-4 only
        res["routes_vs_main_roads"] = route_measures(G_ref, G_scan, n_pairs=n_pairs, ref_max_rank=4)
        T["junctions and routes"] = time.perf_counter() - t
    res["seconds"] = {k: round(v, 1) for k, v in T.items()}
    if out:
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "validation.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
        (out / "validation.md").write_text(markdown(res), encoding="utf-8")
    return res


def markdown(res: dict) -> str:
    L = ["# Image-derived road network: sanity checks against the shapefiles", "",
         f"Network read from the scan only; BD TOPO and OSM used only here. Buffer {res['buffer_m']:g} m, "
         f"lines sampled every {res['step_m']:g} m. Scan network {res['scan_km']} km, BD TOPO {res['bdtopo_km']} km.", "",
         "## Buffer measures", "", "| measure | 15 m | 25 m | 40 m |", "| --- | --- | --- | --- |"]
    for key, name in (("correctness_vs_bdtopo", "correctness vs BD TOPO (scan roads that exist)"),
                      ("completeness_vs_bdtopo_all", "completeness vs BD TOPO, all classes"),
                      ("correctness_vs_osm", "correctness vs OSM"), ("completeness_vs_osm_all", "completeness vs OSM, all classes")):
        if key in res:
            v = res[key]
            L.append(f"| {name} | {v['15m']:.1%} | {v['25m']:.1%} | {v['40m']:.1%} |")
    if "correctness_vs_either" in res:
        L.append(f"\nCorrectness against either shapefile (a scan road is real if BD TOPO or OSM has it): **{res['correctness_vs_either']:.1%}**.")
    L += ["", f"Quality (Wiedemann) all classes {res['quality_all']:.3f}; main roads (BD TOPO importance 1-4 vs blue/red/orange/white symbols) "
          f"{res['quality_main_roads']:.3f}.", "", "### Completeness by BD TOPO class (the 1:100 000 does not draw every lane or track)", "",
          "| BD TOPO class | km | completeness |", "| --- | --- | --- |"]
    for c, v in res["completeness_by_bdtopo_class"].items():
        L.append(f"| {c} | {v['km']} | {v['completeness']:.1%} |")
    L += ["", "### Correctness by map symbol", "", "| symbol | km | correctness |", "| --- | --- | --- |"]
    for s, v in res["correctness_by_symbol"].items():
        L.append(f"| {s} | {v['km']} | {v['correctness']:.1%} |")
    r = res["registration"]
    L += ["", "## Registration", "", f"Median distance of matched points {r['median_distance_m']} m (90th percentile {r['p90_distance_m']} m); "
          f"mean offset {r['mean_offset_east_m']} m east, {r['mean_offset_north_m']} m north.", "",
          "## Map symbol against BD TOPO class (length share of matched scan roads)", "",
          "| symbol | " + " | ".join(BD_CLASS_ORDER) + " |", "| --- |" + " --- |" * len(BD_CLASS_ORDER)]
    for s, row in res["symbol_vs_bdtopo_class"].items():
        L.append(f"| {s} | " + " | ".join(f"{row.get(c, 0):.0%}" for c in BD_CLASS_ORDER) + " |")
    if "junctions" in res and "precision_30m" in res["junctions"]:
        j = res["junctions"]
        L += ["", "## Junctions", "", f"{j['test']} scan junctions (three or more roads, tracks excluded). Precision: share near any of the "
              f"{j['reference_any']} BD TOPO junctions; recall: share of the {j['reference_main']} junctions of BD TOPO importance 1-4 roads "
              f"(all drawn on the map) that have a scan junction near them. Median offset {j['median_offset_m']} m: the map displaces roads "
              f"(cartographic generalisation), so offsets exceed the registration error.", "",
              "| within | precision | recall (importance 1-4) |", "| --- | --- | --- |"]
        for t in ("30m", "60m"):
            L.append(f"| {t} | {j['precision_' + t]:.0%} | {j['recall_main_' + t]:.0%} |")
    if "routes" in res:
        L += ["", "## Routes (APLS-like)", "", "Shortest-path length between the same pairs of junctions on both networks "
              "(score 1 = same lengths, a pair with no scan route scores 0).", "",
              "| reference | pairs | score | median length ratio scan / BD TOPO | within 5 % | within 10 % | no scan route |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for key, name in (("routes", "BD TOPO, every road (importance 1-5)"), ("routes_vs_main_roads", "BD TOPO importance 1-4 only")):
            r = res.get(key)
            if r and r.get("pairs"):
                L.append(f"| {name} | {r['pairs']} | {r['apls_like']} | {r['length_ratio_median']} | {r['within_5pct']:.0%} | "
                         f"{r['within_10pct']:.0%} | {r['test_unreachable']} |")
        r = res["routes"]
        L.append(f"\nPairs: junctions {r.get('distance_km')} km apart that have a scan node within {r.get('snap_m')} m; scan roads of rank "
                 f"<= {r.get('max_rank')} (tracks excluded).")
    if "bridges" in res:
        b = res["bridges"]
        L += ["", "## Bridges", "", f"{b['scan_bridges']} bridges read on the scan (road over water); {b['near_bdtopo_bridge_50m']:.0%} lie "
              f"within 50 m of a BD TOPO bridge section ({b['bdtopo_bridge_sections']} in BD TOPO, which also counts bridges over roads and rail)."]
    if "label_gap_joins" in res:
        g = res["label_gap_joins"]
        L += ["", f"Label gaps joined: {g['km']} km, of which {g['correctness']:.0%} lie on a BD TOPO road."]
    L += ["", "Times (s): " + ", ".join(f"{k} {v}" for k, v in res["seconds"].items()), ""]
    return "\n".join(L)

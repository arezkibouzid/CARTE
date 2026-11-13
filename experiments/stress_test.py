"""Stress tests of the routing pipeline (CPU), every measurement with time, CPU time, memory and energy.

Tests (``--tests``; all by default):
  distance     route length: random origin/destination pairs 5..150 km apart over the whole sheet
  extraction   area: extraction of squares of 500..4000 px (the interface's limit)
  convoys      number of convoys: 1..64 random convoys, deconflicted; and the three deconfliction modes at 16 convoys
  waypoints    free-order waypoints 0..7 (all orders tried: k! permutations)
  avoid        number of zones to avoid: 0..60 random 2 km squares
  combos       40 random combinations of model, objective, lambda, speed, OSM fill, waypoints and avoided zones
  threads      limited resources: the same workload pinned to 1, 2, 4, 8, 16 cores (separate processes)
  degraded     incomplete data (5..40 % of the roads removed at random) and uncertain data (noise on the U-Net
               probabilities): feasibility, detour and route stability on the 10 exercises

    .venv/bin/python experiments/stress_test.py [--tests distance convoys ...] [--quick]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time

import numpy as np

from common import REPO, Run, exercises, peak_rss_mb

SHEET = "hackathon"


def A_():
    from interface.backend import amiad_api as A

    return A


def node_lonlat(N, idx: np.ndarray) -> np.ndarray:
    G = N.G
    return np.array([[G.nodes[N.nodes[i]]["x"], G.nodes[N.nodes[i]]["y"]] for i in idx])


def connected_nodes(N) -> np.ndarray:
    """Nodes of the largest strongly connected part of the network (so that random pairs have a route)."""
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(N.nodes)
    A = csr_matrix((np.ones(len(N.u)), (N.u, N.v)), shape=(n, n))
    _, lab = connected_components(A, directed=True, connection="strong")
    return np.flatnonzero(lab == np.bincount(lab).argmax())


def pairs_at(N, nodes, rng, lo_km, hi_km, k):
    out, tries = [], 0
    while len(out) < k and tries < 20000:
        tries += 1
        a, b = rng.choice(nodes, 2, replace=False)
        d = float(np.hypot(*(N.xy[a] - N.xy[b]))) / 1000
        if lo_km <= d < hi_km:
            out.append((a, b, d))
    return out


def route_req(A, start, end, **kw):
    return A.RouteRequest(start=tuple(start), end=tuple(end), use_bdtopo=False, **kw)


def call_route(R, A, req):
    try:
        resp, m = R.measure(A.route, req)
        r = json.loads(resp.body)
        return r, m
    except Exception as exc:  # noqa: BLE001
        return {"error": str(getattr(exc, "detail", exc))[:200]}, None


# ------------------------------------------------------------------------------------------------- tests

def t_distance(R, A, rng, quick):
    N = A.network(SHEET, "nodice", False)
    nodes = connected_nodes(N)
    for lo, hi in ((3, 7), (8, 12), (20, 30), (45, 55), (90, 110), (140, 170)):
        for a, b, d in pairs_at(N, nodes, rng, lo, hi, 2 if quick else 5):
            ll = node_lonlat(N, [a, b])
            r, m = call_route(R, A, route_req(A, ll[0], ll[1]))
            rec = {"test": "distance", "straight_km": round(d, 1), "feasible": "error" not in r}
            if m:
                rec.update(length_km=r["total"]["length_km"], solve_s=r["timing_s"]["route"], explain_s=r["timing_s"].get("explain"),
                           wall_s=m["wall_s"], cpu_energy_j=m["cpu_energy_j"], rss_mb=m["rss_mb"], segments=sum(len(l["segments"]) for l in r["explanation"]["legs"]))
            R.record(rec)
            R.log("distance", rec)


def t_extraction(R, A, rng, quick):
    r = A.scan_raster(SHEET)
    w, s, e, n = r.bounds
    px_deg_x = (e - w) / r.a.shape[1]
    px_deg_y = (n - s) / r.a.shape[0]
    cx, cy = (w + e) / 2, (s + n) / 2
    for size in ((500, 1500, 3000) if quick else (500, 1000, 2000, 3000, 4000)):
        bb = (cx - size / 2 * px_deg_x, cy - size / 2 * px_deg_y, cx + size / 2 * px_deg_x, cy + size / 2 * px_deg_y)
        resp, m = R.measure(A.extract, A.ExtractRequest(bbox=bb, compare_bdtopo=True))
        x = json.loads(resp.body) if hasattr(resp, "body") else resp
        rec = {"test": "extraction", "size_px": size, "megapixels": round(size * size / 1e6, 2),
               "km": x["stats"]["km"], "sections": x["stats"]["sections"], "wall_s": m["wall_s"], "cpu_energy_j": m["cpu_energy_j"], "rss_mb": m["rss_mb"],
               "completeness": (x["stats"].get("bdtopo") or {}).get("completeness"), "correctness": (x["stats"].get("bdtopo") or {}).get("correctness")}
        R.record(rec)
        R.log("extraction", rec)


def random_convoys(N, nodes, rng, k, min_km=15, max_km=80):
    from interface.backend.amiad_api import Convoy

    cs = []
    for i, (a, b, d) in enumerate(pairs_at(N, nodes, rng, min_km, max_km, k)):
        ll = node_lonlat(N, [a, b])
        cs.append(Convoy(name=f"C{i + 1}", start=tuple(ll[0]), end=tuple(ll[1]), priority=i))
    return cs


def t_convoys(R, A, rng, quick):
    N = A.network(SHEET, "nodice", False)
    nodes = connected_nodes(N)
    pool = random_convoys(N, nodes, rng, 64)
    for k in ((1, 4, 16) if quick else (1, 2, 4, 8, 16, 32, 64)):
        req = A.ConvoysRequest(convoys=pool[:k], deconflict="penalize")
        out, m = R.measure(A.plan_convoys, req)
        rec = {"test": "convoys", "convoys": k, "deconflict": "penalize", "routed": out["summary"]["routed"], "total_km": out["summary"]["total_km"],
               "shared_km": out["summary"]["shared_km"], "wall_s": m["wall_s"], "per_convoy_s": round(m["wall_s"] / k, 3),
               "cpu_energy_j": m["cpu_energy_j"], "rss_mb": m["rss_mb"]}
        R.record(rec)
        R.log("convoys", rec)
    for mode in ("none", "penalize", "forbid"):
        req = A.ConvoysRequest(convoys=pool[:16], deconflict=mode)
        out, m = R.measure(A.plan_convoys, req)
        rec = {"test": "deconflict", "convoys": 16, "deconflict": mode, "routed": out["summary"]["routed"], "total_km": out["summary"]["total_km"],
               "shared_km": out["summary"]["shared_km"], "max_duration_min": out["summary"]["max_duration_min"], "wall_s": m["wall_s"],
               "cpu_energy_j": m["cpu_energy_j"]}
        R.record(rec)
        R.log("deconflict", rec)


def t_waypoints(R, A, rng, quick):
    N = A.network(SHEET, "nodice", False)
    nodes = connected_nodes(N)
    (a, b, _), = pairs_at(N, nodes, rng, 60, 90, 1)
    ab = node_lonlat(N, [a, b])
    mid = (N.xy[a] + N.xy[b]) / 2
    near = nodes[np.argsort(np.hypot(*(N.xy[nodes] - mid).T))[:4000]]
    wps = node_lonlat(N, rng.choice(near, 7, replace=False))
    for k in range(0, (5 if quick else 8)):
        r, m = call_route(R, A, route_req(A, ab[0], ab[1], waypoints=[tuple(w) for w in wps[:k]], waypoint_order="free"))
        rec = {"test": "waypoints", "waypoints": k, "orders_tried": math.factorial(k), "feasible": "error" not in r}
        if m:
            rec.update(length_km=r["total"]["length_km"], solve_s=r["timing_s"]["route"], explain_s=r["timing_s"].get("explain"), wall_s=m["wall_s"],
                       cpu_energy_j=m["cpu_energy_j"])
        R.record(rec)
        R.log("waypoints", rec)


def squares(N, nodes, rng, k, keep_away, size_m=2000):
    """k random squares (lon/lat rings) not covering the points in ``keep_away`` (EPSG:2154)."""
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    out = []
    while len(out) < k:
        c = N.xy[rng.choice(nodes)]
        if min(np.hypot(*(np.asarray(keep_away) - c).T)) < 3000:
            continue
        x0, y0, x1, y1 = c[0] - size_m / 2, c[1] - size_m / 2, c[0] + size_m / 2, c[1] + size_m / 2
        out.append([tuple(tr.transform(x, y)) for x, y in ((x0, y1), (x1, y1), (x1, y0), (x0, y0))])
    return out


def t_avoid(R, A, rng, quick):
    N = A.network(SHEET, "nodice", False)
    nodes = connected_nodes(N)
    (a, b, _), = pairs_at(N, nodes, rng, 60, 90, 1)
    ab = node_lonlat(N, [a, b])
    # zones scattered along the corridor between the two points, so that they actually bite
    mid = (N.xy[a] + N.xy[b]) / 2
    corridor = nodes[np.hypot(*(N.xy[nodes] - mid).T) < 40000]
    allz = squares(N, corridor, rng, 60, [N.xy[a], N.xy[b]])
    base = None
    for k in ((0, 10, 40) if quick else (0, 5, 10, 20, 40, 60)):
        r, m = call_route(R, A, route_req(A, ab[0], ab[1], avoid=allz[:k]))
        rec = {"test": "avoid", "zones": k, "feasible": "error" not in r}
        if m:
            base = base or r["total"]["length_km"]
            rec.update(length_km=r["total"]["length_km"], detour=round(r["total"]["length_km"] / base - 1, 4), solve_s=r["timing_s"]["route"], wall_s=m["wall_s"],
                       cpu_energy_j=m["cpu_energy_j"])
        else:
            rec["error"] = r["error"]
        R.record(rec)
        R.log("avoid", rec)


def t_combos(R, A, rng, quick):
    N = A.network(SHEET, "nodice", False)
    nodes = connected_nodes(N)
    for i in range(10 if quick else 40):
        (a, b, d), = pairs_at(N, nodes, rng, 10, 120, 1)
        ab = node_lonlat(N, [a, b])
        model = str(rng.choice(["nodice", "nodice", "mix12k_tta", "none"]))
        p = {"objective": str(rng.choice(["length", "time"])), "uncertainty_strength": float(rng.choice([0.0, 1.0, 4.0])),
             "speed_cap_kmh": float(rng.choice([50, 80, 110])), "use_osm": bool(rng.random() < 0.4), "model": model,
             "osm_mode": str(rng.choice(["reference", "fill"]))}
        nw = int(rng.choice([0, 0, 3]))
        mid = (N.xy[a] + N.xy[b]) / 2
        near = nodes[np.argsort(np.hypot(*(N.xy[nodes] - mid).T))[:3000]]
        wps = [tuple(x) for x in node_lonlat(N, rng.choice(near, nw, replace=False))] if nw else []
        nz = int(rng.choice([0, 5, 15]))
        zones = squares(N, nodes, rng, nz, [N.xy[a], N.xy[b]] + [N.xy[x] for x in near[:0]]) if nz else []
        r, m = call_route(R, A, route_req(A, ab[0], ab[1], waypoints=wps, waypoint_order="free", avoid=zones, **p))
        rec = {"test": "combos", "i": i, **p, "waypoints": nw, "zones": nz, "straight_km": round(d, 1), "feasible": "error" not in r}
        if m:
            rec.update(length_km=r["total"]["length_km"], wall_s=m["wall_s"], solve_s=r["timing_s"]["route"], cpu_energy_j=m["cpu_energy_j"],
                       uncertainty_applied=r["uncertainty_cost"] is not None)
        else:
            rec["error"] = r["error"]
        R.record(rec)
        R.log("combos", rec)


def worker_threads():
    """Fixed workload for the threads test (run in a subprocess pinned to some cores): warm network, then 5 routes + 8 convoys."""
    A = A_()
    rng = np.random.default_rng(7)
    t0 = time.perf_counter()
    N = A.network(SHEET, "nodice", False)
    A.calibrated_edge_samples(SHEET, "nodice", N)
    t_load = time.perf_counter() - t0
    nodes = connected_nodes(N)
    t1 = time.perf_counter()
    c1 = time.process_time()
    for a, b, _ in pairs_at(N, nodes, rng, 40, 80, 5):
        ll = node_lonlat(N, [a, b])
        A.route(route_req(A, ll[0], ll[1]))
    A.plan_convoys(A.ConvoysRequest(convoys=random_convoys(N, nodes, rng, 8)))
    print(json.dumps({"load_s": round(t_load, 2), "workload_s": round(time.perf_counter() - t1, 2), "workload_cpu_s": round(time.process_time() - c1, 2),
                      "peak_rss_mb": round(peak_rss_mb(), 1)}))


def t_threads(R, A, rng, quick):
    avail = sorted(os.sched_getaffinity(0))
    for k in ((1, 4, 16) if quick else (1, 2, 4, 8, 16)):
        if k > len(avail):
            break
        cores = ",".join(str(c) for c in avail[:k])
        env = dict(os.environ, OMP_NUM_THREADS=str(k), MKL_NUM_THREADS=str(k), OPENBLAS_NUM_THREADS=str(k), CUDA_VISIBLE_DEVICES="")
        t = time.perf_counter()
        p = subprocess.run(["taskset", "-c", cores, sys.executable, __file__, "--worker", "threads"], capture_output=True, text=True, env=env, cwd=REPO)
        wall = time.perf_counter() - t
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            out = {"error": p.stderr[-400:]}
        w = R.cpu.get("watts_per_thread_estimate")
        rec = {"test": "threads", "cores": k, "process_wall_s": round(wall, 2), **out,
               "workload_energy_j_estimated": round(out.get("workload_cpu_s", 0) * w, 1) if w and "workload_cpu_s" in out else None}
        R.record(rec)
        R.log("threads", rec)


def t_degraded(R, A, rng, quick):
    import shapely

    from coa_net import pdfexercises as px

    N = A.network(SHEET, "nodice", False)
    exs = exercises()
    if quick:
        exs = exs[:4]
    owner, pc = A.calibrated_edge_samples(SHEET, "nodice", N)
    p_ref = float(A.calibration_lut(SHEET, "nodice").max())
    rev = np.array([N.eidx.get((int(v), int(u)), -1) for u, v in zip(N.u, N.v)])

    def solve(e, forbid=None, lam=1.0, pcs=None):
        P = e["points"]
        req = A.RouteRequest(start=P[e["start"]], end=P[e["end"]], waypoints=[P[w] for w in e["waypoints"]],
                             waypoint_order="free" if e["waypoint_order"] == "free" else "fixed", objective=e["objective"],
                             round_trip=e["round_trip"], speed_cap_kmh=e["speed_cap_kmh"])
        ex = A.make_exercise(req, A.scan_raster(SHEET).bounds)
        with A._lock:
            N.price = N.base_price.copy()
            N.prob_share_below = None
            N.risk = N.confidence = None
            N.price_by_confidence(owner, pc if pcs is None else pcs, lam, p_ref=p_ref)
            try:
                res = px.solve(ex, N, forbid=forbid)
            except RuntimeError:
                return None
            return res, max(p["snap_m"] for p in res["points"].values())

    def line(res):
        return shapely.union_all([l["_line"] for l in res["legs"]])

    clean, clean_snap = {}, {}
    for e in exs:
        r = solve(e)
        if r:
            clean[e["number"]] = (r[0]["total"]["length_km"], line(r[0]))
            clean_snap[e["number"]] = r[1]

    def score(rec, r, e, key):
        if r is None:
            return
        rec["max_snap_m"] = round(r[1], 1)
        # feasible: a route exists and no point is pushed more than 500 m further from the network than on the intact
        # network (otherwise the route serves another place than the one ordered)
        rec["feasible"] = r[1] <= clean_snap.get(e["number"], 0.0) + 500
        if e["number"] in clean:
            L0, line0 = clean[e["number"]]
            rec.update(length_km=r[0]["total"]["length_km"], detour=round(r[0]["total"]["length_km"] / L0 - 1, 4),
                       **{key: round(A.overlap(line(r[0]), line0), 3)})

    # incomplete data (1): random road sections missing (both directions), e.g. roads the model did not read
    mid_x = (N.xy[N.u] + N.xy[N.v]) / 2
    for frac in ((0.02, 0.05) if quick else (0.01, 0.02, 0.05, 0.1)):
        for rep in range(1 if quick else 3):
            drop = rng.random(len(N.edges)) < frac
            drop |= np.where(rev >= 0, drop[np.maximum(rev, 0)], False)
            for e in exs:
                t = time.perf_counter()
                r = solve(e, drop)
                rec = {"test": "incomplete", "kind": "random sections", "removed_share": frac, "rep": rep, "exercise": e["number"], "feasible": False,
                       "solve_s": round(time.perf_counter() - t, 3)}
                score(rec, r, e, "overlap_with_full")
                R.record(rec)
            R.log("incomplete random", frac, rep, sum(1 for x in R.records if x["test"] == "incomplete" and x["kind"] == "random sections"
                                                      and x["removed_share"] == frac and x["rep"] == rep and x["feasible"]), "/", len(exs))
    # incomplete data (2): unreadable patches of map (disks of 1.5 km radius) covering a share of the sheet
    x0, y0 = N.xy.min(0)
    x1, y1 = N.xy.max(0)
    area = (x1 - x0) * (y1 - y0)
    for frac in ((0.05, 0.2) if quick else (0.05, 0.1, 0.2, 0.3)):
        for rep in range(1 if quick else 3):
            k = int(frac * area / (np.pi * 1500 ** 2))
            cs = np.column_stack([rng.uniform(x0, x1, k), rng.uniform(y0, y1, k)])
            from scipy.spatial import cKDTree

            d, _ = cKDTree(cs).query(mid_x)
            drop = d < 1500
            for e in exs:
                t = time.perf_counter()
                r = solve(e, drop)
                rec = {"test": "incomplete", "kind": "unreadable patches", "removed_share": frac, "rep": rep, "exercise": e["number"], "feasible": False,
                       "sections_removed": round(float(drop.mean()), 3), "solve_s": round(time.perf_counter() - t, 3)}
                score(rec, r, e, "overlap_with_full")
                R.record(rec)
            R.log("incomplete patches", frac, rep, sum(1 for x in R.records if x["test"] == "incomplete" and x["kind"] == "unreadable patches"
                                                       and x["removed_share"] == frac and x["rep"] == rep and x["feasible"]), "/", len(exs))
    # uncertain data: noise on the calibrated probabilities (the network itself unchanged)
    for sigma in ((0.1, 0.2) if quick else (0.05, 0.1, 0.2, 0.3)):
        noisy = np.clip(pc + rng.normal(0, sigma, len(pc)), 0, 1)
        for e in exs:
            r = solve(e, None, 1.0, noisy)
            rec = {"test": "uncertain", "noise_sigma": sigma, "exercise": e["number"], "feasible": False}
            score(rec, r, e, "overlap_with_clean")
            R.record(rec)
        R.log("uncertain", sigma, "done")
    with A._lock:
        N.price = N.base_price.copy()


TESTS = {"distance": t_distance, "extraction": t_extraction, "convoys": t_convoys, "waypoints": t_waypoints, "avoid": t_avoid,
         "combos": t_combos, "threads": t_threads, "degraded": t_degraded}


def summarise(R):
    import pandas as pd

    df = pd.DataFrame(R.records)
    S = {}
    if df.empty:
        return S
    for t, g in df.groupby("test"):
        S[t] = {"runs": len(g), "feasible_share": round(float(g["feasible"].mean()), 3) if "feasible" in g and g["feasible"].notna().any() else None}
    if "convoys" in S:
        g = df[df.test == "convoys"]
        S["convoys"]["scaling_exponent_time_vs_convoys"] = round(float(np.polyfit(np.log(g.convoys), np.log(g.wall_s), 1)[0]), 2)
    if "distance" in S:
        g = df[(df.test == "distance") & df.feasible]
        S["distance"]["scaling_exponent_solve_vs_km"] = round(float(np.polyfit(np.log(g.straight_km), np.log(g.solve_s), 1)[0]), 2)
        S["distance"]["max_solve_s"] = float(g.solve_s.max())
    if "incomplete" in S:
        g = df[df.test == "incomplete"]
        S["incomplete"]["by_kind_and_share"] = {f"{kind} {k}": {"feasible": round(float(v.feasible.mean()), 3),
                                                                 "mean_detour": round(float(v.detour.mean()), 4) if v.detour.notna().any() else None,
                                                                 "mean_overlap_with_full": round(float(v.overlap_with_full.mean()), 3) if v.overlap_with_full.notna().any() else None}
                                                for (kind, k), v in g.groupby(["kind", "removed_share"])}
    if "uncertain" in S:
        g = df[df.test == "uncertain"]
        S["uncertain"]["by_sigma"] = {str(k): {"mean_overlap_with_clean": round(float(v.overlap_with_clean.mean()), 3), "mean_detour": round(float(v.detour.mean()), 4)}
                                      for k, v in g.groupby("noise_sigma")}
    return S


def plots(R):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import pandas as pd

    df = pd.DataFrame(R.records)
    figs = []
    if (df.get("test") == "convoys").any():
        g = df[df.test == "convoys"]
        figs.append(("convoys_scaling.png", lambda ax: (ax.loglog(g.convoys, g.wall_s, "o-"), ax.set_xlabel("convois"), ax.set_ylabel("temps (s)"),
                                                         ax.set_title("Passage à l'échelle : convois"))))
    if (df.get("test") == "distance").any():
        g = df[(df.test == "distance") & df.feasible]
        figs.append(("distance_scaling.png", lambda ax: (ax.scatter(g.straight_km, g.solve_s), ax.set_xlabel("distance à vol d'oiseau (km)"),
                                                          ax.set_ylabel("résolution (s)"), ax.set_title("Temps de calcul / distance"))))
    if (df.get("test") == "waypoints").any():
        g = df[df.test == "waypoints"]
        figs.append(("waypoints_scaling.png", lambda ax: (ax.semilogy(g.waypoints, g.solve_s, "o-"), ax.set_xlabel("étapes (ordre libre)"),
                                                           ax.set_ylabel("résolution (s)"), ax.set_title("Étapes en ordre libre"))))
    if (df.get("test") == "threads").any():
        g = df[df.test == "threads"]
        figs.append(("threads.png", lambda ax: (ax.plot(g.cores, g.workload_s, "o-"), ax.set_xlabel("cœurs CPU"), ax.set_ylabel("charge de travail (s)"),
                                                 ax.set_title("Ressources limitées"))))
    if (df.get("test") == "incomplete").any():
        def inc(ax):
            for kind, mk in (("random sections", "o-"), ("unreadable patches", "s-")):
                g = df[(df.test == "incomplete") & (df.kind == kind)].groupby("removed_share")
                if len(g):
                    ax.plot(g.feasible.mean().index * 100, g.feasible.mean().values, mk, label=f"faisables ({kind})")
            ax.set_xlabel("part supprimée (%)")
            ax.set_ylim(0, 1.05)
            ax.legend(fontsize=7)
            ax.set_title("Données incomplètes")
        figs.append(("incomplete.png", inc))
    for name, f in figs:
        fig, ax = plt.subplots(figsize=(5.5, 3.8))
        f(ax)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(R.dir / name, dpi=130)
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tests", nargs="*", default=list(TESTS))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--worker")
    a = ap.parse_args()
    if a.worker == "threads":
        return worker_threads()
    A = A_()
    cfg = {"script": "experiments/stress_test.py", "tests": a.tests, "quick": a.quick, "seed": a.seed, "sheet": A.sheet_cfg(SHEET),
           "routing_defaults": {"model": "nodice", "uncertainty_strength": 1.0, "objective": "length", "speed_cap_kmh": 80}}
    with Run("stress_test" + ("_quick" if a.quick else ""), cfg) as R:
        rng = np.random.default_rng(a.seed)
        with R.phase("load networks"):
            N = A.network(SHEET, "nodice", False)
            A.calibrated_edge_samples(SHEET, "nodice", N)
            R.summary["network"] = {"nodes": len(N.nodes), "sections": len(N.edges), "km": round(float(N.length_m.sum()) / 2000, 1)}
        for t in a.tests:
            with R.phase(t):
                TESTS[t](R, A, rng, a.quick)
            R.save()
        R.summary.update(summarise(R))
        plots(R)
        R.log(json.dumps(R.summary, indent=1, default=str))


if __name__ == "__main__":
    main()

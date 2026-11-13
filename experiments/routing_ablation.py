"""Routing ablation on the 10 PDF exercises of the Hackathon sheet: contribution of each pipeline component.

Groups (``--groups``):
  network  which road network the route is computed on: the four U-Net models (base / TTA, with / without Dice),
           U-Net completed by OSM, and no model at all (route directly on OSM or on BD TOPO, the reference)
  cost     route cost on the best U-Net network: no uncertainty term, the operator threshold penalty, the probabilistic
           (log-confidence) cost for lambda in 0.25..8, and both together
  explain  cost of the explanation module (segment trace with local alternatives) on solve time

Every route is scored the same way, whatever produced it:
  - against the BD TOPO and OSM routes of the same exercise (same points, area, objective, speed): length / time gap
    and overlap (share of each route within 25 m of the other, averaged both ways);
  - "image confidence": the geometric mean of the calibrated U-Net road probability along the route (nodice model,
    normalised by its plateau), so routes from every source are judged by the same yardstick;
  - feasibility (a route was found), largest snapping distance, solve and explanation time, CPU energy.

    .venv/bin/python experiments/routing_ablation.py [--groups network cost explain] [--exercises 1 2 ...]
"""
from __future__ import annotations

import argparse
import json
import time

import numpy as np

from common import Run, exercises, file_ref

LAMBDAS = [0.1, 0.25, 0.5, 1, 2, 4, 8]
TINY = 1e-6  # "lambda 0" keeps the confidence bookkeeping on, with no effect on the cost


def configs(groups: list[str]) -> list[dict]:
    C = []
    if "network" in groups:
        for m in ("nodice", "nodice_tta", "mix12k_tta", "mix12k"):
            C.append({"name": f"unet_{m}", "group": "network", "model": m, "uncertainty_strength": TINY})
        C.append({"name": "unet_nodice+osm_fill", "group": "network", "model": "nodice", "use_osm": True, "osm_mode": "fill"})
        C.append({"name": "no_model_osm", "group": "network", "model": "none", "use_osm": True})
        C.append({"name": "no_model_bdtopo", "group": "network", "model": "none", "use_osm": False})
    if "cost" in groups:
        C.append({"name": "cost_none", "group": "cost", "model": "nodice", "uncertainty_strength": TINY})
        C.append({"name": "cost_threshold_t0.3_s3", "group": "cost", "model": "nodice", "uncertainty_strength": TINY, "prob_threshold": 0.3, "prob_penalty": 3})
        for l in LAMBDAS:
            C.append({"name": f"cost_lambda_{l:g}", "group": "cost", "model": "nodice", "uncertainty_strength": l})
        C.append({"name": "cost_threshold+lambda_1", "group": "cost", "model": "nodice", "uncertainty_strength": 1, "prob_threshold": 0.3, "prob_penalty": 3})
    if "explain" in groups:
        C.append({"name": "explain_off", "group": "explain", "model": "nodice", "uncertainty_strength": 1, "explain": False})
        C.append({"name": "explain_on", "group": "explain", "model": "nodice", "uncertainty_strength": 1, "explain": True})
    return C


def image_confidence(A, line, p_ref: float) -> float:
    """Geometric mean of the calibrated U-Net probability (nodice) every 20 m along an EPSG:2154 line, normalised."""
    import shapely
    from pyproj import Transformer

    r = A.prob_raster("hackathon", "nodice")
    lut = A.calibration_lut("hackathon", "nodice")
    L = shapely.length(line)
    pts = shapely.get_coordinates(shapely.line_interpolate_point(line, np.arange(10, max(L, 11), 20.0)))
    lon, lat = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True).transform(pts[:, 0], pts[:, 1])
    c = ((np.asarray(lon) - r.tr.c) / r.tr.a).astype(int)
    rr = ((np.asarray(lat) - r.tr.f) / r.tr.e).astype(int)
    p = np.zeros(len(rr))
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            p = np.maximum(p, r.a[np.clip(rr + dr, 0, r.a.shape[0] - 1), np.clip(c + dc, 0, r.a.shape[1] - 1)] / 255.0)
    pc = lut[np.clip(np.round(p * 255), 0, 255).astype(int)]
    return float(np.exp(np.mean(np.log(np.clip(pc / p_ref, 0.05, 1.0)))))


def on_real_roads(A, line, tree_lines, tol=25.0, step=25.0) -> tuple[float, float]:
    """Share of the route within ``tol`` m of a BD TOPO road (any road, not only the reference route), and km beyond."""
    import shapely

    lines, tree = tree_lines
    L = shapely.length(line)
    pts = shapely.line_interpolate_point(line, np.arange(step / 2, max(L, step), step))
    idx = tree.query_nearest(pts, max_distance=tol, return_distance=False)
    near = np.zeros(len(pts), bool)
    near[np.unique(idx[0])] = True
    return float(near.mean()), float((~near).sum() * step / 1000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--groups", nargs="*", default=["network", "cost", "explain"])
    ap.add_argument("--exercises", nargs="*", type=int)
    a = ap.parse_args()
    from interface.backend import amiad_api as A
    from coa_net import pdfexercises as px

    C = configs(a.groups)
    exs = [e for e in exercises() if not a.exercises or e["number"] in a.exercises]
    cfg = {"script": "experiments/routing_ablation.py", "groups": a.groups, "exercises": [e["number"] for e in exs], "configs": C,
           "sheet": A.sheet_cfg("hackathon"), "models": A.sheet_cfg("hackathon")["models"],
           "inputs": [file_ref(A.sheet_cfg("hackathon")[k]) for k in ("tif", "bdtopo", "osm", "pdf")]
                     + [file_ref(str(A.rp(m["raster"]) / "unet_oof.tif")) for m in A.sheet_cfg("hackathon")["models"] if "raster" in m]}
    with Run("routing_ablation", cfg) as R:
        p_ref = float(A.calibration_lut("hackathon", "nodice").max())
        import shapely

        Nb = A.reference_network("hackathon", "bdtopo")
        bd = (Nb.lines, shapely.STRtree(Nb.lines))
        with R.phase("load networks (once, cached afterwards)"):
            for c in C:
                if c["model"] != "none":
                    N = A.network("hackathon", c["model"], bool(c.get("use_osm")) and c.get("osm_mode") == "fill")
                    A.calibrated_edge_samples("hackathon", c["model"], N)
            for w in ("bdtopo", "osm"):
                A.reference_network("hackathon", w)
        refs = {}
        with R.phase("reference routes (BD TOPO, OSM)"):
            for e in exs:
                P = e["points"]
                req = A.RouteRequest(start=P[e["start"]], end=P[e["end"]], waypoints=[P[w] for w in e["waypoints"]],
                                     waypoint_order="free" if e["waypoint_order"] == "free" else "fixed", objective=e["objective"],
                                     round_trip=e["round_trip"], speed_cap_kmh=e["speed_cap_kmh"])
                ex = A.make_exercise(req, A.scan_raster("hackathon").bounds)
                for which in ("bdtopo", "osm"):
                    N = A.reference_network("hackathon", which)
                    with A._lock:
                        N.price = getattr(N, "base_price", N.price).copy()
                        N.risk = N.confidence = None
                        N.prob_share_below = None
                        try:
                            rr = px.solve(ex, N)
                            import shapely

                            refs[(e["number"], which)] = (shapely.union_all([l["_line"] for l in rr["legs"]]), rr["total"])
                        except RuntimeError:
                            refs[(e["number"], which)] = None
                R.log(f"exercise {e['number']}: references ready")
        for c in C:
            for e in exs:
                P = e["points"]
                kw = {k: v for k, v in c.items() if k not in ("name", "group")}
                req = A.RouteRequest(start=P[e["start"]], end=P[e["end"]], waypoints=[P[w] for w in e["waypoints"]],
                                     waypoint_order="free" if e["waypoint_order"] == "free" else "fixed", objective=e["objective"],
                                     round_trip=e["round_trip"], speed_cap_kmh=e["speed_cap_kmh"], use_bdtopo=False, **kw)
                rec = {"config": c["name"], "group": c["group"], "exercise": e["number"]}
                try:
                    with R.phase(c["name"]):
                        resp, m = R.measure(A.route, req)
                    r = json.loads(resp.body)
                except Exception as exc:  # noqa: BLE001 - an infeasible exercise is a result
                    rec.update(feasible=False, error=str(getattr(exc, "detail", exc))[:200])
                    R.record(rec)
                    R.log(c["name"], e["number"], "INFEASIBLE", rec["error"])
                    continue
                ours = A._routes[r["route_id"]][2]
                rec.update(feasible=True, length_km=r["total"]["length_km"], duration_min=r["total"]["duration_min"],
                           solve_s=r["timing_s"].get("route"), explain_s=r["timing_s"].get("explain"), call_wall_s=m["wall_s"],
                           call_cpu_s=m["cpu_s"], cpu_energy_j=m["cpu_energy_j"], energy_method=m["cpu_energy_method"],
                           max_snap_m=max(p["snap_m"] for p in r["points"].values()),
                           model_confidence=r["legs"][0].get("mean_confidence"), uncertainty_cost_share=r["legs"][0].get("uncertainty_cost_share"),
                           image_confidence=round(image_confidence(A, ours, p_ref), 4),
                           km_low_image_confidence=round(sum(l.get("km_low_confidence") or 0 for l in r["legs"]), 2) if r["legs"][0].get("km_low_confidence") is not None else None,
                           network=r["network"], segments=sum(len(l["segments"]) for l in r["explanation"]["legs"]) if r.get("explanation") else None)
                share, off = on_real_roads(A, ours, bd)
                rec.update(on_bdtopo_roads=round(share, 4), km_off_bdtopo_roads=round(off, 2))
                for which in ("bdtopo", "osm"):
                    ref = refs.get((e["number"], which))
                    if ref:
                        line, tot = ref
                        rec[f"gap_{which}"] = round(r["total"]["length_km"] / tot["length_km"] - 1, 4)
                        rec[f"time_gap_{which}"] = round(r["total"]["duration_min"] / tot["duration_min"] - 1, 4)
                        rec[f"overlap_{which}"] = round(A.overlap(ours, line), 4)
                R.record(rec)
                R.log(f"{c['name']:<28} ex {e['number']:>2}: {rec['length_km']:7.2f} km, gap BD TOPO {rec.get('gap_bdtopo', float('nan')):+.3f}, "
                      f"overlap {rec.get('overlap_bdtopo', float('nan')):.2f}, image conf {rec['image_confidence']:.3f}, {m['wall_s']:.2f} s")
        # summary per configuration (mean over exercises; |gap| as the closeness measure)
        summ = {}
        for c in C:
            rs = [x for x in R.records if x["config"] == c["name"]]
            ok = [x for x in rs if x.get("feasible")]

            def mean(k, f=lambda v: v):
                v = [f(x[k]) for x in ok if x.get(k) is not None]
                return round(float(np.mean(v)), 4) if v else None
            summ[c["name"]] = {"group": c["group"], "feasible": f"{len(ok)}/{len(rs)}", "mean_abs_gap_bdtopo": mean("gap_bdtopo", abs),
                               "mean_abs_gap_osm": mean("gap_osm", abs), "mean_gap_bdtopo": mean("gap_bdtopo"), "mean_overlap_bdtopo": mean("overlap_bdtopo"),
                               "mean_overlap_osm": mean("overlap_osm"), "mean_image_confidence": mean("image_confidence"),
                               "mean_on_bdtopo_roads": mean("on_bdtopo_roads"), "total_km_off_bdtopo_roads": round(sum(x.get("km_off_bdtopo_roads") or 0 for x in ok), 2),
                               "mean_length_km": mean("length_km"), "mean_solve_s": mean("solve_s"), "mean_explain_s": mean("explain_s"),
                               "mean_call_s": mean("call_wall_s"), "mean_cpu_energy_j": mean("cpu_energy_j")}
        R.summary = {"per_config": summ, "p_ref": p_ref}
        R.save()
        plots(R, C, summ)
        R.log(json.dumps(summ, indent=1))


def plots(R, C, summ):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    for grp in ("network", "cost"):
        names = [c["name"] for c in C if c["group"] == grp]
        if not names:
            continue
        fig, ax = plt.subplots(1, 3, figsize=(15, 4.2))
        for k, (key, title) in enumerate((("mean_abs_gap_bdtopo", "|écart de longueur| vs BD TOPO"), ("mean_overlap_bdtopo", "tracé commun avec BD TOPO"),
                                          ("mean_on_bdtopo_roads", "part du trajet sur une route réelle"))):
            v = [summ[n][key] or 0 for n in names]
            ax[k].barh(names, v, color="#2b6cb0")
            ax[k].set_title(title)
            ax[k].invert_yaxis()
            for i, x in enumerate(v):
                ax[k].text(x, i, f" {x:.3f}", va="center", fontsize=8)
        fig.tight_layout()
        fig.savefig(R.dir / f"ablation_{grp}.png", dpi=130)
        plt.close(fig)
    lam = [(0.0, "cost_none")] + [(l, f"cost_lambda_{l:g}") for l in LAMBDAS]
    lam = [(l, n) for l, n in lam if n in summ]
    if len(lam) > 2:
        fig, ax1 = plt.subplots(figsize=(6.5, 4.2))
        xs = [l for l, _ in lam]
        ax1.plot(xs, [summ[n]["mean_length_km"] for _, n in lam], "o-", color="#d1495b", label="longueur moyenne (km)")
        ax1.set_xlabel("intensité du coût d'incertitude λ")
        ax1.set_ylabel("longueur moyenne (km)", color="#d1495b")
        ax2 = ax1.twinx()
        ax2.plot(xs, [summ[n]["mean_image_confidence"] for _, n in lam], "s-", color="#2a9d8f", label="confiance image")
        ax2.set_ylabel("confiance image moyenne", color="#2a9d8f")
        ax1.set_xscale("symlog", linthresh=0.25)
        ax1.set_title("Compromis longueur / confiance")
        fig.tight_layout()
        fig.savefig(R.dir / "lambda_tradeoff.png", dpi=130)
        plt.close(fig)


if __name__ == "__main__":
    t = time.time()
    main()

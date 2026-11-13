"""Which network gives the PDF exercises' routes closest to BD TOPO's and OSM's?

Every exercise of the PDF is solved, with the same reading and rules (`coa_net.pdfexercises`), on each candidate
network and on the two references (BD TOPO, OSM). Per exercise and network: length and time gaps against each
reference, and route overlap = mean of (share of the route within 25 m of the reference route) and (share of the
reference route within 25 m of the route).

    python scripts/exercise_ablation.py --data examples --pdf examples/exercise_1-to-10-1.pdf \
        --cache outputs/x/cache --builtup outputs/x/pdf_exercises/builtup_areas.geojson \
        --corrector "no U-Net=outputs/x/learn_full" --corrector "PRC=outputs/x/learn_prc" \
        --alone "PRC alone=outputs/x/prc/finetuned" --out outputs/x/exercise_ablation
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely

from coa_net import pdfexercises as px
from coa_net.scenario import ScenarioPaths, load_graph_cached


def overlap(a, b, tol=25.0, step=10.0) -> float:
    def share(x, y):
        L = shapely.length(x)
        pts = shapely.line_interpolate_point(x, np.arange(0, L, step))
        return float(np.mean(shapely.distance(pts, y) <= tol)) if len(pts) else 0.0

    return (share(a, b) + share(b, a)) / 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--builtup", required=True, help="built-up polygons (GeoJSON) of pdf-exercises")
    ap.add_argument("--corrector", action="append", default=[], metavar="NAME=DIR")
    ap.add_argument("--alone", action="append", default=[], metavar="NAME=DIR", help="folder with unet_oof.tif")
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-osm", action="store_true", help="compare with BD TOPO only")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    tif, cache = paths.scan100, Path(a.cache)
    builtup = gpd.read_file(a.builtup)
    _, exs = px.read_pdf(a.pdf)

    G0, _, _ = load_graph_cached(tif, cache, kind="scan")
    nets = {"image reader only": px.Network(copy.deepcopy(G0), "image", builtup=builtup)}
    from coa_net.scanlearn import correct_graph

    for spec in a.corrector:
        name, d = spec.split("=", 1)
        G = copy.deepcopy(G0)
        correct_graph(G, tif, d)
        nets[f"reader + corrector, {name}"] = px.Network(G, name, builtup=builtup)
    for spec in a.alone:
        from coa_net import scanunet

        name, d = spec.split("=", 1)
        nets[name] = px.Network(scanunet.unet_network(Path(d)), name, builtup=builtup)
    refs = {}
    Gb, _, _ = load_graph_cached(paths.bdtopo, cache)
    refs["BD TOPO"] = px.Network(Gb, "BD TOPO", priced=False, builtup=builtup)
    if paths.osm and not a.no_osm:
        Go, _, _ = load_graph_cached(paths.osm, cache, kind="osm")
        refs["OSM"] = px.Network(Go, "OSM", priced=False, builtup=builtup)
    print("networks:", ", ".join(list(nets) + list(refs)), flush=True)

    def run(N, ex):
        try:
            r = px.solve(ex, N)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
        line = shapely.union_all([l["_line"] for l in r["legs"]])
        return {"km": r["total"]["length_km"], "min": r["total"]["duration_min"], "line": line,
                "snap_max_m": max(v["snap_m"] for v in r["points"].values()), "order": r.get("visit_order")}

    ref_res = {k: {ex.number: run(N, ex) for ex in exs} for k, N in refs.items()}
    rows = []
    for name, N in nets.items():
        for ex in exs:
            r = run(N, ex)
            row = {"network": name, "exercise": ex.number}
            if "error" in r:
                row["error"] = r["error"]
            else:
                row.update(km=r["km"], min=r["min"], snap_max_m=r["snap_max_m"])
                for k, rr in ref_res.items():
                    x = rr[ex.number]
                    row[f"{k} length gap"] = r["km"] / x["km"] - 1
                    row[f"{k} time gap"] = r["min"] / x["min"] - 1
                    row[f"{k} overlap"] = overlap(r["line"], x["line"])
                row["same order as BD TOPO"] = r["order"] == ref_res["BD TOPO"][ex.number]["order"]
            rows.append(row)
            print(name, ex.number, {k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items() if k not in ("network", "exercise")}, flush=True)
    (out / "exercise_ablation.json").write_text(json.dumps(rows, indent=1, default=float), encoding="utf-8")

    import pandas as pd

    df = pd.DataFrame(rows)
    ok = df[df.get("error").isna()] if "error" in df else df
    if "OSM" not in refs:  # BD TOPO only: the OSM columns repeat BD TOPO's
        for c in ("length gap", "time gap", "overlap"):
            ok = ok.assign(**{f"OSM {c}": ok[f"BD TOPO {c}"]})
    agg = ok.groupby("network", sort=False).agg(
        solved=("exercise", "size"),
        bd_len=("BD TOPO length gap", lambda s: s.abs().mean()), bd_time=("BD TOPO time gap", lambda s: s.abs().mean()),
        bd_ov=("BD TOPO overlap", "mean"), osm_len=("OSM length gap", lambda s: s.abs().mean()),
        osm_time=("OSM time gap", lambda s: s.abs().mean()), osm_ov=("OSM overlap", "mean"), snap=("snap_max_m", "max"))
    agg["closeness"] = (agg.bd_ov + agg.osm_ov) / 2 - (agg.bd_len + agg.osm_len + agg.bd_time + agg.osm_time) / 4
    L = ["# Which network gives the exercise routes closest to BD TOPO and OSM", "",
         f"{len(exs)} exercises of `{Path(a.pdf).name}`, same reading and rules on every network (legal limits, built-up 50, "
         "cap from the PDF, deployment area, waypoints, distinct round trips). Gaps are mean absolute; overlap is the "
         "symmetric share of route length within 25 m of the reference route.", "",
         "| network | exercises solved | BD TOPO: length gap | BD TOPO: time gap | BD TOPO: route overlap | OSM: length gap | "
         "OSM: time gap | OSM: route overlap | worst snap (m) | closeness |", "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for n, r in agg.sort_values("closeness", ascending=False).iterrows():
        L.append(f"| {n} | {r.solved}/{len(exs)} | {r.bd_len:.1%} | {r.bd_time:.1%} | {r.bd_ov:.1%} | {r.osm_len:.1%} | "
                 f"{r.osm_time:.1%} | {r.osm_ov:.1%} | {r.snap:.0f} | {r.closeness:.3f} |")
    L += ["", "closeness = mean route overlap - mean absolute gap (higher is better).", "",
          "" if "OSM" not in refs else "BD TOPO against OSM (same exercises): " + ", ".join(
              f"ex {ex.number}: {ref_res['OSM'][ex.number]['km'] / ref_res['BD TOPO'][ex.number]['km'] - 1:+.1%} length, "
              f"overlap {overlap(ref_res['OSM'][ex.number]['line'], ref_res['BD TOPO'][ex.number]['line']):.0%}" for ex in exs), "",
          "## Per exercise (length gap / route overlap against BD TOPO)", "",
          "| network | " + " | ".join(f"ex {ex.number}" for ex in exs) + " |", "| --- |" + " --- |" * len(exs)]
    for n in agg.index:
        cells = []
        for ex in exs:
            r = df[(df.network == n) & (df.exercise == ex.number)].iloc[0]
            cells.append("no route" if isinstance(r.get("error"), str) else f"{r['BD TOPO length gap']:+.0%} / {r['BD TOPO overlap']:.0%}")
        L.append(f"| {n} | " + " | ".join(cells) + " |")
    (out / "exercise_ablation.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

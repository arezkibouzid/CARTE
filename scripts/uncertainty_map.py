"""Where is the U-Net unsure, does that match where it is wrong, and what does an operator's probability threshold do to
the exercise routes?

1. Cells of CELL x CELL pixels over the whole sheet:
   * uncertainty (no labels needed, usable on any sheet): mean binary entropy of the road probability over the pixels with
     some road signal (p >= 0.1);
   * error (labels, out of fold): share of BD TOPO road pixels the U-Net misses (p < 0.5);
   * share of road-signal pixels below the operator's threshold.
   Their agreement (Spearman over cells, lift of the error in the most uncertain cells) says whether the uncertainty can
   stand in for the error on a sheet without labels.
2. Whole-sheet maps of both layers with the 10 exercises' mismatches (mismatch_map.py output) on top; GeoTIFF layers.
3. Threshold sweep: the exercises solved on the U-Net network with sections below the threshold priced
   (1 + strength x share below), against BD TOPO and OSM.

    python scripts/uncertainty_map.py --data examples --pdf examples/exercise_1-to-10-1.pdf --cache C --prc OUT/prc --run mix12k \
        --builtup OUT/pdf_exercises/builtup_areas.geojson --mismatch OUT/mismatch_prc_mix_best --out OUT/uncertainty_mix12k
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely

from coa_net import pdfexercises as px
from coa_net import scanunet
from coa_net.scenario import ScenarioPaths, load_graph_cached

CELL = 64


def entropy(p):
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return -(p * np.log2(p) + (1 - p) * np.log2(1 - p))


def cells(P, road, tau, min_signal=200, min_road=60):
    H, W = road.shape
    h, w = H // CELL, W // CELL
    p = P[: h * CELL, : w * CELL, 0].astype(np.float32) / 255.0
    y = road[: h * CELL, : w * CELL] > 0
    blk = lambda a: a.reshape(h, CELL, w, CELL).swapaxes(1, 2).reshape(h, w, CELL * CELL)
    pb, yb = blk(p), blk(y)
    sig = pb >= 0.1
    nsig = sig.sum(-1)
    U = np.where(nsig >= min_signal, (entropy(pb) * sig).sum(-1) / np.maximum(nsig, 1), np.nan)
    nroad = yb.sum(-1)
    FN = np.where(nroad >= min_road, ((pb < 0.5) & yb).sum(-1) / np.maximum(nroad, 1), np.nan)
    below = np.where(nsig >= min_signal, ((pb < tau) & sig).sum(-1) / np.maximum(nsig, 1), np.nan)
    return U, FN, below


def overlap(a, b, tol=25.0, step=10.0) -> float:
    def share(x, y):
        L = shapely.length(x)
        pts = shapely.line_interpolate_point(x, np.arange(0, L, step))
        return float(np.mean(shapely.distance(pts, y) <= tol)) if len(pts) else 0.0

    return (share(a, b) + share(b, a)) / 2


def main():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from rasterio.enums import Resampling
    from scipy.stats import spearmanr

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--prc", required=True, help="prc folder (road.npy of prep, RUN/unet_oof.tif)")
    ap.add_argument("--run", required=True)
    ap.add_argument("--builtup", required=True)
    ap.add_argument("--mismatch", required=True, help="mismatch_map.py output folder of the same run")
    ap.add_argument("--map-tau", type=float, default=0.7, help="threshold shown on the 'share below' layer")
    ap.add_argument("--taus", type=float, nargs="+", default=[0.6, 0.7, 0.8, 0.9])
    ap.add_argument("--strength", type=float, default=3.0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-sweep", action="store_true", help="maps and uncertainty statistics only")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    tif = paths.scan100
    P, tr = scanunet.load_raster(Path(a.prc) / a.run / "unet_oof.tif")
    road = np.load(Path(a.prc) / "road.npy", mmap_mode="r")

    # ------------------------------------------------------------------ 1. cells
    U, FN, below = cells(P, np.asarray(road), a.map_tau)
    ok = ~np.isnan(U) & ~np.isnan(FN)
    rho = spearmanr(U[ok], FN[ok]).correlation
    q80 = np.nanpercentile(U, 80)
    top = ok & (U >= q80)
    lift = float(np.nanmean(FN[top]) / np.nanmean(FN[ok]))
    stats = {"cells": int(ok.sum()), "spearman_uncertainty_vs_missed": round(float(rho), 3),
             "missed_share_all_cells": round(float(np.nanmean(FN[ok])), 3),
             "missed_share_top20_uncertain": round(float(np.nanmean(FN[top])), 3), "lift_top20": round(lift, 2)}
    # gap points of the exercises in the most uncertain cells
    gp = Path(a.mismatch) / "prc_gaps.geojson"
    if gp.exists():
        g = gpd.read_file(gp)
        g = g[g.reference == "BD TOPO"]
        r, c = rasterio.transform.rowcol(tr, g.geometry.x.values, g.geometry.y.values)
        r, c = np.asarray(r) // CELL, np.asarray(c) // CELL
        inside = (r < U.shape[0]) & (c < U.shape[1])
        ug = U[r[inside], c[inside]]
        stats["gap_points_in_top20_uncertain_cells"] = round(float(np.mean(ug >= q80)), 3)
        stats["gap_points_mean_uncertainty"] = round(float(np.nanmean(ug)), 3)
        stats["sheet_mean_uncertainty"] = round(float(np.nanmean(U)), 3)
    print(stats, flush=True)

    # GeoTIFF layers (cell grid)
    ctr = rasterio.Affine(tr.a * CELL, 0, tr.c, 0, tr.e * CELL, tr.f)
    with rasterio.open(out / "uncertainty_cells.tif", "w", driver="GTiff", height=U.shape[0], width=U.shape[1], count=3, dtype="float32",
                       crs="EPSG:4326", transform=ctr, nodata=np.nan) as dst:
        for i, (arr, name) in enumerate(((U, "uncertainty_entropy"), (FN, "missed_bdtopo_road_share"),
                                         (below, f"share_below_{a.map_tau:g}")), start=1):
            dst.write(arr.astype("float32"), i)
            dst.set_band_description(i, name)

    # ------------------------------------------------------------------ 2. whole-sheet maps
    with rasterio.open(tif) as src:
        f = 8
        A = src.read(1, out_shape=(src.height // f, src.width // f), resampling=Resampling.nearest)
        cm = src.colormap(1)
        lut = np.zeros((256, 3), np.uint8)
        for k, v in cm.items():
            lut[k] = v[:3]
        b = src.bounds
    ext = (b.left, b.right, b.bottom, b.top)
    _, exs = px.read_pdf(a.pdf)
    ex0 = exs[0]
    da = np.array([ex0.points[k] for k in ex0.area] + [ex0.points[ex0.area[0]]])
    mz = gpd.read_file(Path(a.mismatch) / "mismatch_zones.geojson")
    gaps = gpd.read_file(gp) if gp.exists() else None
    k = 1 / math.cos(math.radians((b.bottom + b.top) / 2))
    fig, axs = plt.subplots(2, 1, figsize=(22, 2 * 22 * (b.top - b.bottom) * k / (b.right - b.left) + 2.5), dpi=100)
    layers = [(U, "magma_r", (0.0, 1.0), "uncertainty of the road probability (mean entropy, bits; no labels needed)"),
              (FN, "Reds", (0.0, 0.6), "share of BD TOPO road pixels the U-Net misses (p < 0.5; out of fold, labels)")]
    for ax, (arr, cmap, (vmin, vmax), title) in zip(axs, layers):
        ax.imshow(lut[A], extent=ext, alpha=0.35)
        im = ax.imshow(np.ma.masked_invalid(arr), extent=(tr.c, tr.c + arr.shape[1] * CELL * tr.a, tr.f + arr.shape[0] * CELL * tr.e, tr.f),
                       cmap=cmap, vmin=vmin, vmax=vmax, alpha=0.75, interpolation="nearest")
        ax.plot(da[:, 0], da[:, 1], "--", color="black", lw=1.5, label="deployment area")
        for kind, col, lab in (("our route only", "#00a2ff", f"{a.run} route off BD TOPO / OSM"),
                               ("BD TOPO route only", "#00c853", "BD TOPO route not followed")):
            sub = mz[mz.kind == kind]
            for geom in sub.geometry:
                for part in getattr(geom, "geoms", [geom]):
                    xy = np.asarray(part.coords)
                    ax.plot(xy[:, 0], xy[:, 1], color=col, lw=1.6)
            ax.plot([], [], color=col, lw=2, label=lab)
        if gaps is not None:
            gg = gaps[gaps.reference == "BD TOPO"]
            ax.scatter(gg.geometry.x, gg.geometry.y, s=1.5, color="black", label="gap in the U-Net network (on the BD TOPO route)")
        for e in exs:
            for nme, (x, y) in e.points.items():
                if not nme.startswith("DA"):
                    ax.plot(x, y, "o", color="white", mec="black", ms=6)
                    ax.annotate(nme, (x, y), xytext=(4, 4), textcoords="offset points", fontsize=8, weight="bold")
        ax.set_xlim(b.left, b.right)
        ax.set_ylim(b.bottom, b.top)
        ax.set_aspect(k)
        ax.set_title(title, fontsize=14)
        fig.colorbar(im, ax=ax, fraction=0.015, pad=0.01)
        ax.legend(loc="lower left", fontsize=9, framealpha=0.9, markerscale=6)
    fig.suptitle(f"PRC {a.run}: where the U-Net is unsure (top) and wrong (bottom), with the mismatches of the 10 exercises. "
                 f"Spearman (cells) {stats['spearman_uncertainty_vs_missed']:.2f}; missed roads in the 20% most uncertain cells x{stats['lift_top20']:.1f}",
                 fontsize=14)
    fig.tight_layout()
    fig.savefig(out / "uncertainty_whole_sheet.png")
    plt.close(fig)

    (out / "uncertainty_stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    if a.no_sweep:
        return
    # ------------------------------------------------------------------ 3. threshold sweep on the exercises
    builtup = gpd.read_file(a.builtup)
    N = px.Network(scanunet.unet_network(Path(a.prc) / a.run), a.run, builtup=builtup)
    base_price = N.price.copy()
    refs = {"BD TOPO": px.Network(load_graph_cached(paths.bdtopo, Path(a.cache))[0], "BD TOPO", priced=False, builtup=builtup)}
    if paths.osm:
        refs["OSM"] = px.Network(load_graph_cached(paths.osm, Path(a.cache), kind="osm")[0], "OSM", priced=False, builtup=builtup)

    def solve(net, ex):
        r = px.solve(ex, net)
        return r, shapely.union_all([l["_line"] for l in r["legs"]])

    ref = {k: {ex.number: solve(R, ex) for ex in exs} for k, R in refs.items()}
    rows = []
    for tau in [None] + a.taus:
        N.price = base_price.copy()
        if tau is None:
            N.prob_share_below = None
            pr = {"km_below_threshold": 0.0}
        else:
            pr = N.price_by_probability(P, tr, tau, a.strength)
        for ex in exs:
            r, line = solve(N, ex)
            row = {"tau": tau, "exercise": ex.number, "km": r["total"]["length_km"], "min": r["total"]["duration_min"],
                   "km_below_tau_on_route": sum(l.get("km_below_prob_threshold", 0.0) for l in r["legs"]),
                   "network_km_below_tau": pr["km_below_threshold"]}
            for kk, rr in ref.items():
                rr_, rl = rr[ex.number]
                row[f"{kk} length gap"] = r["total"]["length_km"] / rr_["total"]["length_km"] - 1
                row[f"{kk} time gap"] = r["total"]["duration_min"] / rr_["total"]["duration_min"] - 1
                row[f"{kk} overlap"] = overlap(line, rl)
            rows.append(row)
        print("tau", tau, "done", flush=True)
    (out / "threshold_sweep.json").write_text(json.dumps({"stats": stats, "rows": rows}, indent=1), encoding="utf-8")

    import pandas as pd

    df = pd.DataFrame(rows)
    df["tau_label"] = df["tau"].map(lambda t: "no penalty" if pd.isna(t) else f"{t:g}")
    g = df.groupby("tau_label", sort=False)
    L = [f"# PRC {a.run}: uncertainty, errors, and an operator's probability threshold", "",
         "## 1. Is the uncertainty where the errors are? (cells of 64 x 64 px, about 0.4 x 0.6 km)", "",
         "| measure | value |", "| --- | --- |",
         f"| cells with road signal and BD TOPO road | {stats['cells']:,} |",
         f"| Spearman correlation, uncertainty vs share of BD TOPO road missed | {stats['spearman_uncertainty_vs_missed']:.2f} |",
         f"| BD TOPO road missed, all cells | {stats['missed_share_all_cells']:.1%} |",
         f"| BD TOPO road missed, the 20% most uncertain cells | {stats['missed_share_top20_uncertain']:.1%} (x{stats['lift_top20']:.1f}) |"]
    if "gap_points_in_top20_uncertain_cells" in stats:
        L += [f"| exercise gap points (BD TOPO route) in the 20% most uncertain cells | {stats['gap_points_in_top20_uncertain_cells']:.0%} (20% if unrelated) |",
              f"| mean uncertainty at the gap points / over the sheet | {stats['gap_points_mean_uncertainty']:.2f} / {stats['sheet_mean_uncertainty']:.2f} |"]
    L += ["", "Maps: `uncertainty_whole_sheet.png` (top: uncertainty, no labels; bottom: errors, labels; both with the 10 exercises' "
          "mismatches and gap points). GeoTIFF for QGIS: `uncertainty_cells.tif` (bands: uncertainty, share of BD TOPO road missed, "
          f"share of road-signal pixels below {a.map_tau:g}).", "",
          f"## 2. Operator's threshold: sections below it cost 1 + {a.strength:g} x the share below (mean over the 10 exercises)", "",
          "| threshold | network km below it | route km below it (mean) | length gap vs BD TOPO | time gap vs BD TOPO | overlap with BD TOPO | "
          "length gap vs OSM | overlap with OSM |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for lab, d in g:
        L.append(f"| {lab} | {d['network_km_below_tau'].iloc[0]:,.0f} | {d['km_below_tau_on_route'].mean():.1f} | "
                 f"{d['BD TOPO length gap'].abs().mean():.1%} | {d['BD TOPO time gap'].abs().mean():.1%} | {d['BD TOPO overlap'].mean():.1%} | "
                 + (f"{d['OSM length gap'].abs().mean():.1%} | {d['OSM overlap'].mean():.1%} |" if "OSM overlap" in d else "- | - |"))
    L += ["", "### Per exercise (length gap / overlap against BD TOPO)", "",
          "| threshold | " + " | ".join(f"ex {e.number}" for e in exs) + " |", "| --- |" + " --- |" * len(exs)]
    for lab, d in g:
        L.append(f"| {lab} | " + " | ".join(f"{x:+.1%} / {o:.0%}" for x, o in zip(d["BD TOPO length gap"], d["BD TOPO overlap"])) + " |")
    (out / "uncertainty_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

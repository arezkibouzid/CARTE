"""Ablation of the learned corrector of the image network, on any sheet (table 2.1 of
docs/corrector_route_gate_ablation.md).

Every variant applies the same out-of-fold predictions of a `coa-net scan-learn` folder; only the corrections
switched on and their thresholds change. Routes (fastest convoy route, 60 km/h cap, rank 5 or better) are compared
with BD TOPO's on two pair sets:

* town pairs: every pair of the 15 densest BD TOPO road clusters (towns), at least 15 km apart;
* random pairs: points 15-70 km apart, drawn with a seed other than the route gate's.

P = pricing of doubtful roads and label gaps, W = white roads to rank 4, L = missing links, G = learned displacement.

    python scripts/scan_ablation.py --data examples --cache outputs/x/cache --learn outputs/x/learn_full \
        [--learn-unet "U-Net=outputs/x/learn_unet" ...] [--alone "U-Net=outputs/x/unet" ...] --out outputs/x/ablation
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import time
from pathlib import Path

import numpy as np

from coa_net import scanlearn as sl
from coa_net.scancheck import _points, _seg_lengths, buffer_measures, graph_lines, reference_lines
from coa_net.scanroute import _csgraph, route_table, score
from coa_net.scenario import ScenarioPaths, load_graph_cached

W, L, CROSS, BRIDGE = "white_to_rank4", "add_link", "separate_crossing", "bridge_to_culvert"
SKIP_NATURE = {"Sentier", "Chemin", "Escalier", "Piste cyclable", "Route empierrée"}


def km(a, b):
    return math.hypot((a[0] - b[0]) * 111.32 * math.cos(math.radians(a[1])), (a[1] - b[1]) * 110.57)


def towns(bdtopo: str, n: int = 15, cell_km: float = 2.0, min_sep_km: float = 10.0) -> list[tuple]:
    """Town centres: the densest 2 km cells of BD TOPO roads (no paths or tracks), at least 10 km apart."""
    import pyogrio

    g = pyogrio.read_dataframe(bdtopo, columns=["NATURE"])
    g = g[~g["NATURE"].isin(SKIP_NATURE)]
    c = g.geometry.centroid
    x, y = c.x.values, c.y.values
    k = math.cos(math.radians(float(np.mean(y))))
    cell = cell_km / 111.0
    ix, iy = ((x - x.min()) * k / cell).astype(int), ((y - y.min()) / cell).astype(int)
    H = np.zeros((ix.max() + 1, iy.max() + 1))
    np.add.at(H, (ix, iy), 1)
    picks: list[tuple] = []
    for o in np.argsort(H.ravel())[::-1]:
        i, j = np.unravel_index(o, H.shape)
        m = (ix == i) & (iy == j)
        p = (float(np.median(x[m])), float(np.median(y[m])))
        if all(km(p, q) >= min_sep_km for q in picks):
            picks.append(p)
        if len(picks) == n:
            break
    return picks


def town_pairs(T, min_km=15.0):
    return [(a, b) for i, a in enumerate(T) for b in T[i + 1:] if km(a, b) >= min_km]


def random_pairs(bounds, n=150, min_km=15.0, max_km=70.0, seed=12345):
    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = bounds
    out = []
    while len(out) < n:
        a = (rng.uniform(x0 + .02, x1 - .02), rng.uniform(y0 + .02, y1 - .02))
        b = (rng.uniform(x0 + .02, x1 - .02), rng.uniform(y0 + .02, y1 - .02))
        if min_km <= km(a, b) <= max_km:
            out.append((a, b))
    return out


def snap_km(G, pairs):
    """Distance (km) from each end of each pair to the routable part of G (largest strongly connected network of
    rank 5 or better), shape (n, 2)."""
    from scipy.spatial import cKDTree

    xy = _csgraph(G, False)[2]
    k = np.array([111.32 * math.cos(math.radians(float(xy[:, 1].mean()))), 110.57])
    tree = cKDTree(xy * k)
    return np.column_stack([tree.query(np.array([p[i] for p in pairs]) * k)[0] for i in (0, 1)])


def reached(G, pairs, ref_km, slack_km=1.0):
    """Share of pairs whose two ends are at most ``slack_km`` farther from G's routable network than from BD TOPO's:
    the others are snapped onto a far-away fragment and their routes mean nothing."""
    return float(np.mean((snap_km(G, pairs) <= ref_km + slack_km).all(1)))


def gaps(img, bd):
    ok = ~(np.isnan(img).any(1) | np.isnan(bd).any(1))
    return {"pairs": int(ok.sum()), "time_gap": float(img[ok, 0].sum() / bd[ok, 0].sum() - 1),
            "length_gap": float(np.median(img[ok, 1] / bd[ok, 1]) - 1), "score": score(img, bd)}


class Within25:
    """Share of a network's length within 25 m of BD TOPO (the correctness of scan-roads' checks)."""

    def __init__(self, bdtopo, osm):
        bd, _ = reference_lines(bdtopo, osm)
        self.bxy, _ = _points(bd.geometry.values, 10.0)

    def __call__(self, G):
        sc = graph_lines(G)
        sxy, sidx = _points(sc.geometry.values, 10.0)
        return float(buffer_measures(sxy, _seg_lengths(sxy, sidx), self.bxy)[0]["25m"])


def load_oof(G, folder: Path):
    Pj = json.loads((folder / "oof_predictions.json").read_text(encoding="utf-8"))
    nt = type(next(iter(G.nodes)))
    return {"roads": Pj["roads"], "crossings": {int(k): v for k, v in Pj["crossings"].items()},
            "crossing_nodes": [nt(n) for n in Pj["crossing_nodes"]], "links": Pj.get("links") or []}


def variants_of(folder: Path, tag: str, unet: bool) -> list[tuple]:
    """(name, folder, enabled, thresholds, move lines) of each variant."""
    meta = json.loads((folder / "corrector.json").read_text(encoding="utf-8"))
    gate_th = dict(meta["thresholds"], uncertain_road=sl.THRESH["uncertain_road"])
    road_th = dict(sl.THRESH)
    full = set(meta["enabled"]) - sl.REMOVALS - {"move_lines"}
    G_on = "move_lines" in meta["enabled"]
    th_txt = f"{gate_th['link']} / {gate_th['imp4']}"
    if unet:
        return [
            (f"{tag}: P + L", folder, {L}, road_th, False),
            (f"{tag}: P + W", folder, {W}, road_th, False),
            (f"{tag}: full, thresholds of the corrector without U-Net (0.5 / 0.7 road gate)", folder, full | {L, W}, road_th, G_on),
            (f"{tag}: full, as gated (link / imp4 = {th_txt}; on: {sorted(full) or 'nothing'})", folder, full, gate_th, G_on),
            (f"{tag}: full + bridge and flyover decisions", folder, full | {CROSS, BRIDGE}, gate_th, G_on),
        ]
    return [
        ("P", folder, set(), road_th, False),
        ("P + removals", folder, set(sl.REMOVALS), road_th, False),
        ("P + W (road-gate threshold)", folder, {W}, road_th, False),
        ("P + L (road-gate threshold)", folder, {L}, road_th, False),
        ("P + L + W, road-gate thresholds", folder, {L, W}, road_th, False),
        (f"P + L + W, route-gate thresholds ({th_txt})", folder, {L, W}, gate_th, False),
        (f"full, as gated (on: {sorted(full) or 'nothing'}{', G' if G_on else ''})", folder, full, gate_th, G_on),
        ("full without L", folder, full - {L}, gate_th, G_on),
        ("full without W", folder, full - {W}, gate_th, G_on),
        ("full without G", folder, full, gate_th, False),
        ("full + CNN decisions (bridge to culvert, flyovers)", folder, full | {CROSS, BRIDGE}, gate_th, G_on),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--learn", required=True, help="scan-learn folder (corrector without U-Net)")
    ap.add_argument("--learn-unet", action="append", default=[], metavar="NAME=DIR",
                    help="scan-learn --unet folder, with the name of its segmentation network (repeatable)")
    ap.add_argument("--alone", action="append", default=[], metavar="NAME=DIR",
                    help="folder with a unet_oof.tif: its own network, no reader, no corrector (repeatable)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    tif = paths.scan100
    cache = Path(a.cache)
    G0, _, _ = load_graph_cached(tif, cache, kind="scan")
    G_bd, _, _ = load_graph_cached(paths.bdtopo, cache)
    xs = [d["x"] for _, d in G_bd.nodes(data=True)]
    ys = [d["y"] for _, d in G_bd.nodes(data=True)]
    bounds = (min(xs), min(ys), max(xs), max(ys))
    T = towns(paths.bdtopo)
    tp, rp = town_pairs(T), random_pairs(bounds)
    print(f"{len(T)} towns, {len(tp)} town pairs, {len(rp)} random pairs", flush=True)
    bd_t, bd_r = route_table(G_bd, tp, priced=False), route_table(G_bd, rp, priced=False)
    w25 = Within25(paths.bdtopo, paths.osm)
    ref_km = snap_km(G_bd, tp + rp)

    rows = []

    def measure(name, G, ch=None):
        t = time.perf_counter()
        r = {"variant": name, "white_to_rank4": (ch or {}).get("white roads to rank 4", 0),
             "links_added": (ch or {}).get("links added", 0),
             "reached": reached(G, tp + rp, ref_km), "town": gaps(route_table(G, tp), bd_t), "random": gaps(route_table(G, rp), bd_r), "within_25m": w25(G)}
        rows.append(r)
        print(f"{name}: reached {r['reached']:.0%}, town time {r['town']['time_gap']:+.1%} length {r['town']['length_gap']:+.1%}, random time "
              f"{r['random']['time_gap']:+.1%}, score {r['town']['score']:.3f} / {r['random']['score']:.3f}, "
              f"25 m {r['within_25m']:.1%} ({time.perf_counter() - t:.0f} s)", flush=True)
        (out / "ablation.json").write_text(json.dumps({"towns": T, "rows": rows}, indent=1), encoding="utf-8")

    measure("none (image network, no corrector)", G0)
    todo = variants_of(Path(a.learn), "", False)
    for spec in a.learn_unet:
        name, d = spec.split("=", 1)
        todo += variants_of(Path(d), f"{name} features", True)
    oof = {}
    for name, folder, on, th, move in todo:
        G = copy.deepcopy(G0)
        P = oof.setdefault(folder, load_oof(G0, folder))
        ch = sl.apply_corrections(G, P, th, on)
        if move:
            from coa_net import scangeom

            ch.update(scangeom.move_lines(G, json.loads((folder / "geometry_oof_offsets.json").read_text(encoding="utf-8"))))
        measure(name, G, ch)
    for spec in a.alone:
        from coa_net import scanunet

        name, d = spec.split("=", 1)
        Pu, tr = scanunet.load_raster(Path(d) / "unet_oof.tif")
        measure(f"{name} alone: its own network, no reader, no corrector", scanunet.network_from_unet(Pu, tr))

    md = ["# Corrector ablation on " + Path(tif).name, "",
          f"Out-of-fold predictions of `{a.learn}`" + ("".join(f", `{x}`" for x in a.learn_unet)) +
          f". {len(tp)} town pairs ({len(T)} towns = densest BD TOPO road clusters), {len(rp)} random pairs 15-70 km apart. "
          "Gap = total time / median length of the image routes over BD TOPO's. Score = mean |log time ratio| + mean |log length ratio|.", "",
          "| variant | white roads to rank 4 | links added | town: time gap | town: length gap | random: time gap | random: length gap "
          "| route score, town / random | network within 25 m of BD TOPO | pairs reached |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        if r["reached"] < 0.9:
            md.append(f"| {r['variant']} | - | - | not routable | | | | | {r['within_25m']:.1%} | {r['reached']:.0%} |")
            continue
        md.append(f"| {r['variant']} | {r['white_to_rank4']:,} | {r['links_added']:,} | {r['town']['time_gap']:+.1%} | "
                  f"{r['town']['length_gap']:+.1%} | {r['random']['time_gap']:+.1%} | {r['random']['length_gap']:+.1%} | "
                  f"{r['town']['score']:.3f} / {r['random']['score']:.3f} | {r['within_25m']:.1%} | {r['reached']:.0%} |")
    md += ["", "Towns (lon, lat): " + ", ".join(f"({x:.3f}, {y:.3f})" for x, y in T), ""]
    (out / "ablation.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()

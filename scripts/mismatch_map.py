"""Where do the PRC network's exercise routes differ from BD TOPO's and OSM's, and how sure was PRC there?

Every exercise of the PDF is solved (same rules as `coa-net pdf-exercises`) on the PRC network (`network_from_unet` of
an out-of-fold PRC raster), on BD TOPO and on OSM. The routes are sampled every 10 m:

* "ours only": our route more than 25 m from the reference route; "reference only": the reference route more than
  25 m from ours. Runs of at least 200 m are kept and grouped into zones (within 1 km of each other).
* In each zone the PRC road probability (out of fold: no block is scored by a model that saw its labels) is read along
  our road and along the reference road we did not take, with the share of that reference road covered by a PRC
  network edge (within 25 m). The cause of the mismatch:
    - "road missed by PRC": p < 0.35 on the reference road;
    - "PRC uncertain": 0.35 <= p < 0.65;
    - "road seen, broken in the PRC network": p >= 0.65 but less than 80% of it covered by a PRC edge;
    - "alternative chosen": the reference road is seen and connected; PRC's network made another road shorter/faster.

Outputs (OUT): mismatch_exNN.png (routes coloured by agreement, zones numbered), zones_exNN.png (zone details with the
PRC probability), mismatch_zones.geojson, mismatch_summary.md, mismatch.json.

    python scripts/mismatch_map.py --data examples --pdf examples/exercise_1-to-10-1.pdf --cache C \
        --builtup outputs/x/pdf_exercises/builtup_areas.geojson --unet outputs/x/prc/finetuned --out outputs/x/mismatch_prc6k
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from pyproj import Transformer

from coa_net import pdfexercises as px
from coa_net import scanunet
from coa_net.scenario import ScenarioPaths, load_graph_cached

TOL, STEP, MIN_RUN, ZONE_GAP = 25.0, 10.0, 200.0, 1000.0
TO_LL = Transformer.from_crs(px.L93, "EPSG:4326", always_xy=True)


def sample(line: shapely.LineString) -> np.ndarray:
    L = line.length
    return shapely.get_coordinates(shapely.line_interpolate_point(line, np.arange(0, L + 1e-6, STEP)))


def runs(flag: np.ndarray) -> list[tuple[int, int]]:
    out, i, n = [], 0, len(flag)
    while i < n:
        if flag[i]:
            j = i
            while j + 1 < n and flag[j + 1]:
                j += 1
            if (j - i + 1) * STEP >= MIN_RUN:
                out.append((i, j + 1))
            i = j + 1
        else:
            i += 1
    return out


class Prob:
    """PRC road probability (out of fold) at Lambert-93 points."""

    def __init__(self, P, tr):
        self.p, self.tr = P[..., 0], tr

    def at(self, xy: np.ndarray) -> np.ndarray:
        lon, lat = TO_LL.transform(xy[:, 0], xy[:, 1])
        c = ((np.asarray(lon) - self.tr.c) / self.tr.a).astype(int)
        r = ((np.asarray(lat) - self.tr.f) / self.tr.e).astype(int)
        ok = (r >= 0) & (r < self.p.shape[0]) & (c >= 0) & (c < self.p.shape[1])
        v = np.full(len(xy), np.nan)
        # max over a 3 x 3 window: the label and the centre line can sit a pixel apart
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                rr, cc = np.clip(r + dr, 0, self.p.shape[0] - 1), np.clip(c + dc, 0, self.p.shape[1] - 1)
                v[ok] = np.fmax(v[ok], self.p[rr[ok], cc[ok]] / 255.0)
        return v


def corridor_connected(N, net_tree, seg: np.ndarray, width: float = 40.0, end_r: float = 150.0) -> bool:
    """Does the PRC network join the two ends of ``seg`` without leaving a ``width`` corridor around it?"""
    import networkx as nx

    line = shapely.LineString(seg)
    corr = shapely.union_all([line.buffer(width), shapely.Point(seg[0]).buffer(end_r), shapely.Point(seg[-1]).buffer(end_r)])
    cand = net_tree.query(corr, predicate="intersects")
    if not len(cand):
        return False
    inside = shapely.length(shapely.intersection(N.lines[cand], corr)) / np.maximum(shapely.length(N.lines[cand]), 1e-6)
    keep = cand[inside >= 0.8]
    if not len(keep):
        return False
    H = nx.Graph()
    H.add_edges_from(zip(N.u[keep].tolist(), N.v[keep].tolist()))
    nodes = np.array(list(H.nodes))
    xy = N.xy[nodes]
    a = nodes[np.argmin(np.hypot(*(xy - seg[0]).T))]
    b = nodes[np.argmin(np.hypot(*(xy - seg[-1]).T))]
    if min(np.hypot(*(N.xy[a] - seg[0])), np.hypot(*(N.xy[b] - seg[-1]))) > end_r:
        return False
    return nx.has_path(H, a, b)


def segments(ours_lines, ref_lines, kind: str, prob: Prob, net_tree, N=None) -> list[dict]:
    """Runs of ``ours_lines`` farther than TOL from ``ref_lines`` (kind 'ours only') or the reverse."""
    src, other = (ours_lines, ref_lines) if kind == "ours" else (ref_lines, ours_lines)
    other_u = shapely.union_all(other)
    out = []
    for line in src:
        pts = sample(line)
        if len(pts) < 2:
            continue
        d = shapely.distance(shapely.points(pts), other_u)
        for a, b in runs(d > TOL):
            seg = pts[a:b]
            p = prob.at(seg)
            cov, gaps, conn = None, np.zeros((0, 2)), None
            if kind == "ref":
                _, dd = net_tree.query_nearest(shapely.points(seg), return_distance=True, all_matches=False)
                cov = float(np.mean(dd <= TOL))
                gaps = seg[dd > TOL]
                conn = corridor_connected(N, net_tree, seg)
            out.append({"kind": kind, "geom": shapely.LineString(seg), "km": (b - a) * STEP / 1000,
                        "p_mean": float(np.nanmean(p)), "p_min": float(np.nanmin(p)), "p_low_share": float(np.mean(p < 0.5)),
                        "net_cover": cov, "gaps": gaps, "connected": conn})
    return out


def zones(segs: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for s in sorted(segs, key=lambda s: -s["km"]):
        hit = [g for g in groups if any(s["geom"].distance(t["geom"]) <= ZONE_GAP for t in g)]
        if not hit:
            groups.append([s])
        else:
            hit[0].append(s)
            for g in hit[1:]:
                hit[0].extend(g)
                groups.remove(g)
    return groups


def cause(refs: list[dict], ours_km: float = 0.0) -> str:
    """By the PRC probability along the reference road PRC did not follow, then by whether the PRC network joins that
    road's two ends inside a 40 m corridor (connected: PRC had the road and preferred its own way)."""
    if not refs:
        return "PRC detour only (the reference stays on our route)"
    km = sum(r["km"] for r in refs)
    p = sum(r["p_mean"] * r["km"] for r in refs) / km
    if p < 0.35:
        return "road missed by PRC"
    if p < 0.65:
        return "PRC uncertain"
    broken = sum(r["km"] for r in refs if r.get("connected") is False)
    if broken >= 0.5 * km:
        return "road seen, broken in the PRC network"
    return "alternative chosen (road seen and connected in PRC)"


def ll(geom) -> np.ndarray:
    xy = shapely.get_coordinates(geom)
    return np.column_stack(TO_LL.transform(xy[:, 0], xy[:, 1]))


def scan_crop(tif, x0, y0, x1, y1, max_px=2500):
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    with rasterio.open(tif) as src:
        w = from_bounds(x0, y0, x1, y1, src.transform).round_offsets().round_lengths()
        f = max(1, int(max(w.width, w.height) // max_px))
        a = src.read(1, window=w, out_shape=(max(int(w.height // f), 1), max(int(w.width // f), 1)), resampling=Resampling.nearest, boundless=True)
        cm = src.colormap(1)
        lut = np.zeros((256, 3), np.uint8)
        for k, v in cm.items():
            lut[k] = v[:3]
        b = rasterio.windows.bounds(w, src.transform)
    return lut[a], (b[0], b[2], b[1], b[3])


def prob_crop(P, tr, x0, y0, x1, y1):
    c0, c1 = int((x0 - tr.c) / tr.a), int((x1 - tr.c) / tr.a)
    r0, r1 = int((y1 - tr.f) / tr.e), int((y0 - tr.f) / tr.e)
    c0, r0 = max(c0, 0), max(r0, 0)
    a = P[r0:r1, c0:c1, 0] / 255.0
    return a, (tr.c + c0 * tr.a, tr.c + c1 * tr.a, tr.f + r1 * tr.e, tr.f + r0 * tr.e)


def main():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--builtup", required=True)
    ap.add_argument("--unet", required=True, help="folder with the out-of-fold PRC raster unet_oof.tif")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    tif, cache = paths.scan100, Path(a.cache)
    builtup = gpd.read_file(a.builtup)
    _, exs = px.read_pdf(a.pdf)
    P, tr = scanunet.load_raster(Path(a.unet) / "unet_oof.tif")
    prob = Prob(P, tr)
    N = px.Network(scanunet.unet_network(Path(a.unet)), "PRC", builtup=builtup)
    net_tree = shapely.STRtree(N.lines)
    refs = {"BD TOPO": px.Network(load_graph_cached(paths.bdtopo, cache)[0], "BD TOPO", priced=False, builtup=builtup)}
    if paths.osm:
        refs["OSM"] = px.Network(load_graph_cached(paths.osm, cache, kind="osm")[0], "OSM", priced=False, builtup=builtup)

    rows, feats, summary, gap_feats = [], [], [], []
    for ex in exs:
        ours = px.solve(ex, N)
        o_lines = [l["_line"] for l in ours["legs"]]
        ref_lines = {k: [l["_line"] for l in px.solve(ex, R)["legs"]] for k, R in refs.items()}
        o_pts = np.vstack([sample(l) for l in o_lines])
        dist = {k: shapely.distance(shapely.points(o_pts), shapely.union_all(v)) for k, v in ref_lines.items()}
        status = np.where((dist["BD TOPO"] > TOL) & (dist.get("OSM", dist["BD TOPO"]) > TOL), 3,
                          np.where(dist["BD TOPO"] > TOL, 1, np.where(dist.get("OSM", dist["BD TOPO"]) > TOL, 2, 0)))
        p_ours = prob.at(o_pts)
        ex_sum = {"exercise": ex.number, "objective": ex.objective, "route_km": ours["total"]["length_km"],
                  "km_agree_both": float((status == 0).sum() * STEP / 1000), "km_off_bd_only": float((status == 1).sum() * STEP / 1000),
                  "km_off_osm_only": float((status == 2).sum() * STEP / 1000), "km_off_both": float((status == 3).sum() * STEP / 1000),
                  "p_route_mean": float(np.nanmean(p_ours)), "p_route_low_share": float(np.mean(p_ours < 0.5)), "zones": []}
        segs = []
        for k, v in ref_lines.items():
            for s in segments(o_lines, v, "ours", prob, net_tree, N) + segments(o_lines, v, "ref", prob, net_tree, N):
                s["ref"] = k
                segs.append(s)
        zs = zones(segs)
        zs.sort(key=lambda g: -sum(s["km"] for s in g if s["kind"] == "ours"))
        for i, g in enumerate(zs, start=1):
            env = shapely.union_all([s["geom"] for s in g]).envelope
            cx, cy = TO_LL.transform(env.centroid.x, env.centroid.y)
            z = {"zone": i, "lon": round(cx, 4), "lat": round(cy, 4),
                 "ours_km": round(max([sum(s["km"] for s in g if s["kind"] == "ours" and s["ref"] == r) for r in refs] or [0]), 2)}
            for r in refs:
                rr = [s for s in g if s["kind"] == "ref" and s["ref"] == r]
                oo = [s for s in g if s["kind"] == "ours" and s["ref"] == r]
                z[f"{r} km"] = round(sum(s["km"] for s in rr), 2)
                z[f"p on {r} road"] = round(sum(s["p_mean"] * s["km"] for s in rr) / max(sum(s["km"] for s in rr), 1e-9), 2) if rr else None
                z[f"PRC edge cover of {r} road"] = round(sum((s["net_cover"] or 0) * s["km"] for s in rr) / max(sum(s["km"] for s in rr), 1e-9), 2) if rr else None
                z[f"p on our road vs {r}"] = round(sum(s["p_mean"] * s["km"] for s in oo) / max(sum(s["km"] for s in oo), 1e-9), 2) if oo else None
                z[f"cause vs {r}"] = cause(rr, sum(s["km"] for s in oo)) if (rr or oo) else "agrees"
                gp = [s["gaps"] for s in rr if len(s["gaps"])]
                z[f"gap km on {r} road"] = round(sum(len(x) for x in gp) * STEP / 1000, 2)
            ex_sum["zones"].append(z)
            for s in g:
                if s["kind"] == "ref" and len(s["gaps"]):
                    pg = prob.at(s["gaps"])
                    for (gx_, gy_), pv in zip(s["gaps"], pg):
                        gap_feats.append({"exercise": ex.number, "zone": i, "reference": s["ref"], "p_road": round(float(pv), 3),
                                          "geometry": shapely.Point(gx_, gy_)})
                feats.append({"exercise": ex.number, "zone": i, "kind": "our route only" if s["kind"] == "ours" else f"{s['ref']} route only",
                              "reference": s["ref"], "km": round(s["km"], 2), "p_mean": round(s["p_mean"], 3), "p_min": round(s["p_min"], 3),
                              "p_below_0_5": round(s["p_low_share"], 3), "prc_edge_cover": s["net_cover"],
                              "gap_km": round(len(s["gaps"]) * STEP / 1000, 2), "geometry": s["geom"]})
        summary.append(ex_sum)

        # --- overview map: our route coloured by agreement, the references, numbered zones
        allg = o_lines + [l for v in ref_lines.values() for l in v]
        xy = np.vstack([ll(g) for g in allg])
        x0, y0 = xy.min(0) - 0.03
        x1, y1 = xy.max(0) + 0.03
        img, ext = scan_crop(tif, x0, y0, x1, y1)
        k = 1 / math.cos(math.radians((y0 + y1) / 2))
        fig, ax = plt.subplots(figsize=(16, 16 * (y1 - y0) * k / (x1 - x0) + 1.2), dpi=110)
        ax.imshow(img, extent=ext, alpha=0.55)
        colours = {"BD TOPO": "#1f5fd6", "OSM": "#1a9641"}
        for r, v in ref_lines.items():
            for l in v:
                c = ll(l)
                ax.plot(c[:, 0], c[:, 1], "--" if r == "BD TOPO" else ":", color=colours[r], lw=2.4, alpha=0.9)
            ax.plot([], [], "--" if r == "BD TOPO" else ":", color=colours[r], lw=2.4, label=f"{r} route")
        oll = np.column_stack(TO_LL.transform(o_pts[:, 0], o_pts[:, 1]))
        scol = {0: "#222222", 1: "#ff8c00", 2: "#9b30ff", 3: "#e31a1c"}
        slab = {0: "PRC route = BD TOPO and OSM", 1: "PRC route off BD TOPO only", 2: "PRC route off OSM only", 3: "PRC route off both"}
        for s_, c in scol.items():
            m = status == s_
            ax.scatter(oll[m, 0], oll[m, 1], s=5, color=c, zorder=4, label=f"{slab[s_]} ({m.sum() * STEP / 1000:.1f} km)")
        gaps_all = [s["gaps"] for s in segs if s["kind"] == "ref" and len(s["gaps"])]
        if gaps_all:
            gx = np.vstack(gaps_all)
            gll = np.column_stack(TO_LL.transform(gx[:, 0], gx[:, 1]))
            ax.scatter(gll[:, 0], gll[:, 1], marker="x", s=14, color="black", zorder=5, linewidths=0.8,
                       label=f"reference road with no PRC edge within 25 m ({len(gx) * STEP / 1000:.1f} km)")
        for z, g in zip(ex_sum["zones"], zs):
            env = shapely.union_all([s["geom"] for s in g]).envelope.buffer(400).envelope
            c = ll(env)
            ax.plot(c[:, 0], c[:, 1], color="#e31a1c", lw=1.5)
            ax.annotate(str(z["zone"]), (c[:, 0].max(), c[:, 1].max()), fontsize=13, weight="bold", color="#e31a1c",
                        bbox=dict(boxstyle="round,pad=0.15", fc="white", alpha=0.85))
        for nme, (x, y) in ex.points.items():
            if not nme.startswith("DA"):
                ax.plot(x, y, "o", color="black", ms=8, mec="white", zorder=6)
                ax.annotate(nme, (x, y), xytext=(6, 6), textcoords="offset points", fontsize=11, weight="bold")
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        ax.set_aspect(k)
        ax.set_title(f"Exercise {ex.number}: PRC 6k route against BD TOPO and OSM - {len(zs)} mismatch zones "
                     f"(off BD TOPO {ex_sum['km_off_bd_only'] + ex_sum['km_off_both']:.1f} km, off OSM {ex_sum['km_off_osm_only'] + ex_sum['km_off_both']:.1f} km "
                     f"of {ours['total']['length_km']:.0f} km)", fontsize=13)
        ax.legend(loc="lower left", fontsize=10, markerscale=3, framealpha=0.9)
        fig.tight_layout()
        fig.savefig(out / f"mismatch_ex{ex.number:02d}.png")
        plt.close(fig)

        # --- zone details: scan + PRC probability, the routes
        top = list(zip(ex_sum["zones"], zs))[:6]
        if top:
            n = len(top)
            cols = min(3, n)
            rws = math.ceil(n / cols)
            fig, axs = plt.subplots(rws, cols, figsize=(6.5 * cols, 6.2 * rws), dpi=100, squeeze=False)
            for axz, (z, g) in zip(axs.ravel(), top):
                env = shapely.union_all([s["geom"] for s in g]).envelope.buffer(700).envelope
                c = ll(env)
                zx0, zy0 = c.min(0)
                zx1, zy1 = c.max(0)
                im, ext = scan_crop(tif, zx0, zy0, zx1, zy1, max_px=1200)
                axz.imshow(im, extent=ext, alpha=0.6)
                pa, pext = prob_crop(P, tr, zx0, zy0, zx1, zy1)
                rgba = plt.get_cmap("magma_r")(pa)
                rgba[..., 3] = np.clip(pa * 0.85, 0, 0.85)
                hm = axz.imshow(rgba, extent=pext)
                for r, v in ref_lines.items():
                    for l in v:
                        cc = ll(l)
                        axz.plot(cc[:, 0], cc[:, 1], "--" if r == "BD TOPO" else ":", color=colours[r], lw=2.5)
                for o in o_lines:
                    cc = ll(o)
                    axz.plot(cc[:, 0], cc[:, 1], color="#e31a1c", lw=2.5, alpha=0.85)
                gz = [s["gaps"] for s in g if s["kind"] == "ref" and len(s["gaps"])]
                if gz:
                    gx = np.vstack(gz)
                    gll = np.column_stack(TO_LL.transform(gx[:, 0], gx[:, 1]))
                    axz.scatter(gll[:, 0], gll[:, 1], marker="x", s=22, color="black", zorder=6, linewidths=1.0)
                axz.set_xlim(zx0, zx1)
                axz.set_ylim(zy0, zy1)
                axz.set_aspect(1 / math.cos(math.radians((zy0 + zy1) / 2)))
                axz.set_xticks([])
                axz.set_yticks([])
                causes = sorted({z[f"cause vs {r}"] for r in refs if z.get(f"cause vs {r}") != "agrees"})
                pb = z.get("p on BD TOPO road")
                axz.set_title(f"zone {z['zone']} ({z['lat']:.3f} N, {z['lon']:.3f} E): ours {z['ours_km']:.1f} km\n"
                              + (f"p on BD TOPO road {pb:.2f}, gap {z.get('gap km on BD TOPO road', 0):.1f} km; " if pb is not None else "")
                              + "; ".join(causes), fontsize=9)
            for axz in axs.ravel()[n:]:
                axz.axis("off")
            fig.suptitle(f"Exercise {ex.number}: mismatch zones. Red: PRC route; blue dashed: BD TOPO; green dotted: OSM; "
                         "x: reference road with no PRC edge; heat: PRC road probability (out of fold, dark = high)", fontsize=11)
            fig.tight_layout()
            fig.savefig(out / f"zones_ex{ex.number:02d}.png")
            plt.close(fig)
        print(f"exercise {ex.number}: {len(zs)} zones, off BD TOPO {ex_sum['km_off_bd_only'] + ex_sum['km_off_both']:.1f} km, "
              f"off OSM {ex_sum['km_off_osm_only'] + ex_sum['km_off_both']:.1f} km, off both {ex_sum['km_off_both']:.1f} km", flush=True)

    gpd.GeoDataFrame(feats, geometry="geometry", crs=px.L93).to_crs("EPSG:4326").to_file(out / "mismatch_zones.geojson", driver="GeoJSON")
    (out / "mismatch.json").write_text(json.dumps(summary, indent=1, default=float), encoding="utf-8")
    gdf = gpd.GeoDataFrame(gap_feats, geometry="geometry", crs=px.L93)
    if len(gdf):
        gdf["built_up"] = gdf.within(shapely.union_all(builtup.to_crs(px.L93).geometry.values))
        gdf.to_crs("EPSG:4326").to_file(out / "prc_gaps.geojson", driver="GeoJSON")
    gaps = [(s_["exercise"], z) for s_ in summary for z in s_["zones"]]

    # --- summary
    L = ["# Where the PRC 6k routes differ from BD TOPO and OSM", "",
         f"Routes sampled every {STEP:.0f} m; a stretch counts as different when it is more than {TOL:.0f} m from the other route for at least "
         f"{MIN_RUN:.0f} m. PRC road probability from the out-of-fold raster (max over 3 x 3 pixels). Causes: road missed by PRC (p < 0.35 on the "
         "reference road), PRC uncertain (0.35-0.65), road seen but broken in the PRC network (p >= 0.65 but the PRC network does not join the two ends "
         "of the reference road inside a 40 m corridor), alternative chosen (seen and joined: PRC had the road and preferred its own way). "
         "Gap km = reference road with no PRC network edge within 25 m.", "",
         "## Per exercise", "",
         "| exercise | objective | route km | agrees with both | off BD TOPO only | off OSM only | off both | mean p on our route | zones |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for s in summary:
        L.append(f"| {s['exercise']} | {'shortest' if s['objective'] == 'length' else 'fastest'} | {s['route_km']:.1f} | {s['km_agree_both']:.1f} km "
                 f"| {s['km_off_bd_only']:.1f} km | {s['km_off_osm_only']:.1f} km | {s['km_off_both']:.1f} km | {s['p_route_mean']:.2f} | {len(s['zones'])} |")
    allz = [(s["exercise"], z) for s in summary for z in s["zones"]]
    cnt: dict = {}
    for _, z in allz:
        for r in refs:
            c = z.get(f"cause vs {r}")
            if c and c != "agrees":
                cnt.setdefault((r, c), [0, 0.0])
                cnt[(r, c)][0] += 1
                cnt[(r, c)][1] += z[f"{r} km"] if z.get(f"{r} km") else 0
    L += ["", "## Causes (all zones of all exercises)", "", "| reference | cause | zones | reference km not followed |", "| --- | --- | --- | --- |"]
    for (r, c), (n, km) in sorted(cnt.items(), key=lambda kv: (kv[0][0], -kv[1][0])):
        L.append(f"| {r} | {c} | {n} | {km:.1f} |")
    if len(gdf):
        gb = gdf.groupby("reference")
        L += ["", "## Gap points (reference road with no PRC edge within 25 m, every 10 m; `prc_gaps.geojson`)", "",
              "| reference | gap km | in built-up areas | mean p_road at the gap | gaps with p < 0.5 |", "| --- | --- | --- | --- | --- |"]
        for r, g in gb:
            L.append(f"| {r} | {len(g) * STEP / 1000:.1f} | {g['built_up'].mean():.0%} | {g['p_road'].mean():.2f} | {(g['p_road'] < 0.5).mean():.0%} |")
    L += ["", "## Zones", "", "| exercise | zone | where (lat, lon) | our km | BD TOPO km | p on BD TOPO road | gap km (BD TOPO) | cause vs BD TOPO | OSM km | p on OSM road | gap km (OSM) | cause vs OSM |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    f = lambda v: "-" if v is None else f"{v:.2f}"
    for e, z in allz:
        L.append(f"| {e} | {z['zone']} | {z['lat']:.3f}, {z['lon']:.3f} | {z['ours_km']:.1f} | {z.get('BD TOPO km', 0):.1f} | {f(z.get('p on BD TOPO road'))} | "
                 f"{z.get('gap km on BD TOPO road', 0):.1f} | {z.get('cause vs BD TOPO', '-')} | {z.get('OSM km', 0):.1f} | {f(z.get('p on OSM road'))} | "
                 f"{z.get('gap km on OSM road', 0):.1f} | {z.get('cause vs OSM', '-')} |")
    (out / "mismatch_summary.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L[:40]))


if __name__ == "__main__":
    main()

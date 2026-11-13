"""Are the links added by gap bridging real roads? Every link of a traced network (`unet_bridge` edges) is checked
against BD TOPO and OSM: share of its length within TOL m of a road a convoy can use (BD TOPO without paths, tracks and
stone tracks: the U-Net's own label; OSM drivable classes without tracks), of a track or path only, or of nothing.

    python scripts/check_bridges.py --data examples --network outputs/x/retrace/bridge300 --out outputs/x/bridge_check_300
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
from pathlib import Path

import geopandas as gpd
import numpy as np
import shapely
from scipy.spatial import cKDTree

from coa_net.realdata import OSM_DRIVABLE, read_lines
from coa_net.scanunet import NOT_ROAD, load_raster
from coa_net.scenario import ScenarioPaths

TOL, STEP = 25.0, 5.0
L93 = "EPSG:2154"


def dense_tree(geoms, step=STEP):
    pts = []
    for g in geoms:
        for part in getattr(g, "geoms", [g]):
            L = part.length
            pts.append(shapely.get_coordinates(shapely.line_interpolate_point(part, np.arange(0, L + 1e-6, step))))
    return cKDTree(np.vstack(pts)) if pts else None


def share_within(tree, pts):
    if tree is None:
        return 0.0
    d, _ = tree.query(pts, distance_upper_bound=TOL + 1)
    return float(np.mean(d <= TOL))


def main():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from pyproj import Transformer
    from rasterio.windows import from_bounds

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--network", required=True, help="folder with network.pkl (retrace.py) and unet_oof.tif")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    G = pickle.load(open(Path(a.network) / "network.pkl", "rb"))
    to93 = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    links = []
    for u, v, d in G.edges(data=True):
        if d.get("unet_bridge") and (u < v or not G.has_edge(v, u)):
            c = np.asarray(d["coords"])
            x, y = to93.transform(c[:, 0], c[:, 1])
            links.append({"u": u, "v": v, "p_mean": d.get("unet_bridge_p", np.nan), "length_m": d["length_m"],
                          "geom": shapely.LineString(np.column_stack([x, y]))})
    print(f"{len(links)} bridge links", flush=True)

    bd, _ = read_lines(paths.bdtopo, "cp1252", ["NATURE", "IMPORTANCE"])
    bd = bd.to_crs(L93)
    nat = bd["NATURE"].fillna("").str.lower()
    road_bd = dense_tree(bd.geometry[~nat.isin(NOT_ROAD)].values)
    any_bd = dense_tree(bd.geometry.values)
    road_osm = any_osm = None
    if paths.osm:
        om, _ = read_lines(paths.osm, "utf-8", ["fclass"])
        om = om.to_crs(L93)
        drivable = om["fclass"].isin(OSM_DRIVABLE) & ~om["fclass"].fillna("").str.startswith("track")
        road_osm, any_osm = dense_tree(om.geometry[drivable].values), dense_tree(om.geometry.values)

    for l in links:
        g = l["geom"]
        pts = shapely.get_coordinates(shapely.line_interpolate_point(g, np.arange(0, g.length + 1e-6, STEP)))
        l["bd_road"], l["bd_any"] = share_within(road_bd, pts), share_within(any_bd, pts)
        l["osm_road"], l["osm_any"] = share_within(road_osm, pts), share_within(any_osm, pts)
        s = l["bd_road"]
        l["class"] = ("on a BD TOPO road" if s >= 0.8 else "track or path only" if l["bd_any"] >= 0.8 else
                      "partly on a road" if s >= 0.3 else "no road")
        l["osm_confirms"] = l["osm_road"] >= 0.8
    gdf = gpd.GeoDataFrame([{k: v for k, v in l.items() if k != "geom"} for l in links], geometry=[l["geom"] for l in links], crs=L93)
    gdf.to_crs("EPSG:4326").to_file(out / "bridges_checked.geojson", driver="GeoJSON")

    df = gdf.drop(columns="geometry")
    km = df["length_m"].sum() / 1000
    order = ["on a BD TOPO road", "partly on a road", "track or path only", "no road"]
    L = [f"# Are the bridging links real roads? `{Path(a.network).name}`", "",
         f"{len(df):,} links, {km:.1f} km. A link is on a road when at least 80% of its length lies within {TOL:.0f} m of a BD TOPO road a "
         "convoy can use (paths, tracks and stone tracks excluded, as in the U-Net's labels); 'track or path only' when it lies on a BD TOPO "
         "track or path instead; 'partly' at 30-80%; 'no road' below 30%. OSM (drivable classes, tracks excluded) is a second, independent check.", "",
         "| class | links | km | share of links | OSM agrees it is a road |", "| --- | --- | --- | --- | --- |"]
    for c in order:
        d = df[df["class"] == c]
        L.append(f"| {c} | {len(d):,} | {d['length_m'].sum() / 1000:.1f} | {len(d) / max(len(df), 1):.0%} | {d['osm_confirms'].mean() if len(d) else 0:.0%} |")
    prec = (df["class"] == "on a BD TOPO road").mean()
    prec_any = df["class"].isin(["on a BD TOPO road", "partly on a road"]).mean()
    L += ["", f"**Precision**: {prec:.0%} of the links are on a BD TOPO road ({prec_any:.0%} at least partly); "
              f"{(df['class'] == 'track or path only').mean():.0%} join a track or path; {(df['class'] == 'no road').mean():.0%} cross no road. "
              f"OSM confirms {df['osm_confirms'].mean():.0%} of all links.", "",
          "## By the link's mean U-Net probability (the operator can raise the bridging threshold)", "",
          "| mean probability along the link | links | on a BD TOPO road | at least partly | no road |", "| --- | --- | --- | --- | --- |"]
    for lo, hi in ((0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01)):
        d = df[(df["p_mean"] >= lo) & (df["p_mean"] < hi)]
        if len(d):
            L.append(f"| {lo:.1f}-{min(hi, 1):.1f} | {len(d):,} | {(d['class'] == 'on a BD TOPO road').mean():.0%} | "
                     f"{d['class'].isin(order[:2]).mean():.0%} | {(d['class'] == 'no road').mean():.0%} |")
    L += ["", "## By link length", "", "| length | links | on a BD TOPO road | at least partly | no road |", "| --- | --- | --- | --- | --- |"]
    for lo, hi in ((0, 50), (50, 100), (100, 150), (150, 200), (200, 300)):
        d = df[(df["length_m"] >= lo) & (df["length_m"] < hi)]
        if len(d):
            L.append(f"| {lo}-{hi} m | {len(d):,} | {(d['class'] == 'on a BD TOPO road').mean():.0%} | "
                     f"{d['class'].isin(order[:2]).mean():.0%} | {(d['class'] == 'no road').mean():.0%} |")

    # example close-ups: 6 on a road, 6 with no road (the longest of each, for visibility)
    tif = paths.scan100
    P, tr = load_raster(Path(a.network) / "unet_oof.tif")
    to_ll = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    ex = [r for c in ("on a BD TOPO road", "no road") for _, r in gdf[gdf["class"] == c].sort_values("length_m", ascending=False).head(6).iterrows()]
    if ex:
        fig, axs = plt.subplots(2, 6, figsize=(30, 10.5), dpi=90, squeeze=False)
        bd_ll = bd.to_crs("EPSG:4326")
        with rasterio.open(tif) as src:
            cm = src.colormap(1)
            lut = np.zeros((256, 3), np.uint8)
            for k, v in cm.items():
                lut[k] = v[:3]
            for ax, r in zip(axs.ravel(), ex):
                cx, cy = to_ll.transform(r.geometry.centroid.x, r.geometry.centroid.y)
                h = 0.004
                x0, x1, y0, y1 = cx - h, cx + h, cy - h * 0.66, cy + h * 0.66
                w = from_bounds(x0, y0, x1, y1, src.transform)
                img = src.read(1, window=w, boundless=True)
                ax.imshow(lut[img], extent=(x0, x1, y0, y1))
                c0, c1 = int((x0 - tr.c) / tr.a), int((x1 - tr.c) / tr.a)
                r0, r1 = int((y1 - tr.f) / tr.e), int((y0 - tr.f) / tr.e)
                pa = P[max(r0, 0):r1, max(c0, 0):c1, 0] / 255.0
                rgba = plt.get_cmap("magma_r")(pa)
                rgba[..., 3] = np.clip(pa * 0.6, 0, 0.6)
                ax.imshow(rgba, extent=(x0, x1, y0, y1))
                for geom in bd_ll.cx[x0:x1, y0:y1].geometry:
                    for part in getattr(geom, "geoms", [geom]):
                        xy = np.asarray(part.coords)
                        ax.plot(xy[:, 0], xy[:, 1], color="#1f5fd6", lw=1.2, alpha=0.8)
                xy = np.column_stack(to_ll.transform(*np.asarray(r.geometry.coords).T))
                ax.plot(xy[:, 0], xy[:, 1], color="#00c853" if r["class"] == "on a BD TOPO road" else "#e31a1c", lw=4)
                ax.set_xlim(x0, x1)
                ax.set_ylim(y0, y1)
                ax.set_aspect(1 / math.cos(math.radians(cy)))
                ax.set_xticks([])
                ax.set_yticks([])
                ax.set_title(f"{r['class']}: {r.length_m:.0f} m, mean p {r.p_mean:.2f}\nBD TOPO road {r.bd_road:.0%}, OSM road {r.osm_road:.0%}", fontsize=10)
        fig.suptitle("Bridging links: green = on a BD TOPO road, red = no road; blue = BD TOPO lines; heat = U-Net road probability", fontsize=13)
        fig.tight_layout()
        fig.savefig(out / "bridge_examples.png")
        plt.close(fig)
    L += ["", "Files: `bridges_checked.geojson` (every link with its class and shares, for QGIS), `bridge_examples.png` (the longest links "
          "on a road and with no road)."]
    (out / "bridge_check.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "bridge_check.json").write_text(json.dumps({"links": len(df), "km": km, "precision_road": prec, "precision_partly": prec_any,
                                                       "osm_confirms": float(df["osm_confirms"].mean())}, indent=1), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

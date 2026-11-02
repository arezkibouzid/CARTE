"""Data audit of a Scenario delivery: what the files are, whether they agree, how well the scans line up.

Every number is measured on the files; nothing is assumed beyond the CRS
inference documented in :mod:`coa_net.realdata`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from .realdata import L93


def _lines_l93(gdf):
    return gdf.to_crs(L93).geometry.values


def _sample(lines, step_m: float):
    import shapely

    pts, owner = [], []
    for i, g in enumerate(lines):
        n = max(1, int(g.length // step_m))
        d = (np.arange(n) + 0.5) * g.length / n
        pts.append(shapely.line_interpolate_point(g, d))
        owner.append(np.full(n, i))
    return np.concatenate(pts), np.concatenate(owner)


def coverage(lines_a, lines_b, tol_m: float = 20.0, step_m: float = 50.0) -> float:
    """Share of the length of ``lines_a`` lying within ``tol_m`` of ``lines_b``."""
    import shapely

    pts, _ = _sample(lines_a, step_m)
    tree = shapely.STRtree(lines_b)
    hit = tree.query(pts, predicate="dwithin", distance=tol_m)
    return float(len(np.unique(hit[0])) / len(pts)) if len(pts) else float("nan")


def raster_alignment(tif: str, lines_wgs, max_shift_px: int = 6, step_m: float = 40.0) -> dict:
    """Misregistration of a palette scan against vector roads.

    Road-ink colours are learnt from the data (palette indices over-represented
    under the roads at zero shift); the shift that maximises the share of
    samples on road ink estimates the offset.
    """
    import geopandas as gpd
    import rasterio
    import shapely

    g = gpd.GeoSeries(lines_wgs, crs="EPSG:4326").to_crs(L93)
    pts, _ = _sample(g.values, step_m)
    p = gpd.GeoSeries(pts, crs=L93).to_crs("EPSG:4326")
    xs, ys = p.x.values, p.y.values
    with rasterio.open(tif) as src:
        rows, cols = rasterio.transform.rowcol(src.transform, xs, ys)
        rows, cols = np.asarray(rows), np.asarray(cols)
        a = src.read(1)
        res_m = abs(src.transform.a) * 111_320 * np.cos(np.radians(ys.mean()))
    H, W = a.shape
    ok = (rows >= max_shift_px) & (rows < H - max_shift_px) & (cols >= max_shift_px) & (cols < W - max_shift_px)
    rows, cols = rows[ok], cols[ok]
    under = np.bincount(a[rows, cols], minlength=256) / len(rows)
    rng = np.random.default_rng(0)
    rr, cc = rng.integers(0, H, 200000), rng.integers(0, W, 200000)
    glob = np.bincount(a[rr, cc], minlength=256) / len(rr)
    ink = np.flatnonzero((under > 0.002) & (under > 3 * (glob + 1e-4)))
    is_ink = np.isin(np.arange(256), ink)
    grid = {}
    for dy in range(-max_shift_px, max_shift_px + 1):
        for dx in range(-max_shift_px, max_shift_px + 1):
            grid[(dx, dy)] = float(is_ink[a[rows + dy, cols + dx]].mean())
    (bx, by), best = max(grid.items(), key=lambda kv: kv[1])
    return {"samples": int(len(rows)), "ink_classes": ink.tolist(), "on_ink_at_zero": round(grid[(0, 0)], 3),
            "best_shift_px": [bx, by], "on_ink_at_best": round(best, 3),
            "best_shift_m": [round(bx * res_m, 1), round(-by * abs(src.transform.e) * 111_320, 1)], "pixel_m": round(res_m, 2)}


def road_ink(a: np.ndarray, rows: np.ndarray, cols: np.ndarray, n_random: int = 200_000) -> np.ndarray:
    """Palette indices over-represented under known roads (the road symbols of the scan): boolean mask over 0..255."""
    under = np.bincount(a[rows, cols], minlength=256) / max(1, len(rows))
    rng = np.random.default_rng(0)
    H, W = a.shape
    glob = np.bincount(a[rng.integers(0, H, n_random), rng.integers(0, W, n_random)], minlength=256) / n_random
    return (under > 0.002) & (under > 3 * (glob + 1e-4))


def scan_agreement(tif: str, lines_l93, ref_lines_l93, tol_px: int = 1, step_m: float = 25.0) -> list[float]:
    """Image check of routes: for each line of ``lines_l93``, share of points every ``step_m``
    that fall on road ink of the scan (within ``tol_px`` pixels).

    The road-ink colours are learnt from ``ref_lines_l93`` (e.g. the classified
    roads of the vector data), so the check needs no legend."""
    import geopandas as gpd
    import rasterio

    def rc(lines, src):
        pts, owner = _sample(lines, step_m)
        p = gpd.GeoSeries(pts, crs=L93).to_crs("EPSG:4326")
        r, c = rasterio.transform.rowcol(src.transform, p.x.values, p.y.values)
        return np.asarray(r), np.asarray(c), owner

    with rasterio.open(tif) as src:
        a = src.read(1)
        H, W = a.shape
        r0, c0, _ = rc(list(ref_lines_l93), src)
        ok = (r0 >= 0) & (r0 < H) & (c0 >= 0) & (c0 < W)
        ink = road_ink(a, r0[ok], c0[ok])
        r, c, owner = rc(list(lines_l93), src)
    hit = np.zeros(len(r), bool)
    for dy in range(-tol_px, tol_px + 1):
        for dx in range(-tol_px, tol_px + 1):
            rr, cc = np.clip(r + dy, 0, H - 1), np.clip(c + dx, 0, W - 1)
            hit |= ink[a[rr, cc]]
    return [round(float(hit[owner == i].mean()), 3) if (owner == i).any() else float("nan") for i in range(len(lines_l93))]


def audit(paths, out: str | Path | None = None) -> dict:
    import geopandas as gpd
    import pyogrio
    import rasterio

    rep: dict = {"files": {}}
    t0 = time.perf_counter()
    for key, path, enc in (("bdtopo", paths.bdtopo, "cp1252"), ("osm", paths.osm, "utf-8")):
        if not path:
            continue
        info = pyogrio.read_info(path, encoding=enc)
        has_prj = Path(path).with_suffix(".prj").exists()
        rep["files"][key] = {"path": path, "features": int(info["features"]), "geometry": info["geometry_type"],
                             "crs_in_file": info["crs"], "prj": has_prj, "cpg": Path(path).with_suffix(".cpg").exists(),
                             "bounds": [round(float(b), 5) for b in info["total_bounds"]], "fields": list(info["fields"])}
    for key, path in (("scan100", paths.scan100), ("scan250", paths.scan250)):
        if not path:
            continue
        with rasterio.open(path) as r:
            rep["files"][key] = {"path": path, "size_px": [r.width, r.height], "bands": r.count, "dtype": r.dtypes[0],
                                 "crs": str(r.crs), "pixel_deg": [r.res[0], r.res[1]], "bounds": [round(b, 5) for b in r.bounds],
                                 "palette": r.colorinterp[0].name == "palette", "software": r.tags().get("TIFFTAG_SOFTWARE")}
    # vector agreement on classified roads and bridges
    bd = gpd.read_file(paths.bdtopo, encoding="cp1252", columns=["NATURE", "IMPORTANCE", "POS_SOL", "FRANCHISST"]).set_crs("EPSG:4326")
    osm = gpd.read_file(paths.osm, encoding="utf-8", columns=["fclass", "bridge"]).set_crs("EPSG:4326")
    bd_major = bd[bd["IMPORTANCE"].isin(["1", "2", "3", "4"])]
    osm_major = osm[osm["fclass"].str.replace("_link", "").isin(["motorway", "trunk", "primary", "secondary", "tertiary"])]
    bd_drive = bd[~bd["NATURE"].str.lower().isin(["sentier", "escalier", "piste cyclable"])]
    from .realdata import OSM_DRIVABLE

    osm_drive = osm[osm["fclass"].isin(OSM_DRIVABLE)]
    L_bd_major, L_osm_major = _lines_l93(bd_major), _lines_l93(osm_major)
    L_bd_drive, L_osm_drive = _lines_l93(bd_drive), _lines_l93(osm_drive)
    rep["agreement"] = {
        "bd_major_km": round(float(sum(g.length for g in L_bd_major)) / 1000), "osm_major_km": round(float(sum(g.length for g in L_osm_major)) / 1000),
        "bd_drivable_km": round(float(sum(g.length for g in L_bd_drive)) / 1000), "osm_drivable_km": round(float(sum(g.length for g in L_osm_drive)) / 1000),
        "bd_major_within_20m_of_osm_drivable": round(coverage(L_bd_major, L_osm_drive), 3),
        "osm_major_within_20m_of_bd_drivable": round(coverage(L_osm_major, L_bd_drive), 3),
        "bd_drivable_within_20m_of_osm_drivable": round(coverage(L_bd_drive, L_osm_drive, step_m=200), 3),
        "osm_drivable_within_20m_of_bd_drivable": round(coverage(L_osm_drive, L_bd_drive, step_m=200), 3),
    }
    bd_br = bd_drive[(bd_drive["POS_SOL"] > 0) | (bd_drive["FRANCHISST"] == "Pont")]
    osm_br = osm_drive[osm_drive["bridge"] == "T"]
    import shapely

    tb = shapely.STRtree(_lines_l93(osm_br))
    bdb = _lines_l93(bd_br)
    hit = tb.query(bdb, predicate="dwithin", distance=30.0)
    rep["bridges"] = {"bdtopo_drivable_bridge_sections": int(len(bd_br)), "osm_drivable_bridge_ways": int(len(osm_br)),
                      "bdtopo_bridges_with_osm_bridge_within_30m": round(len(np.unique(hit[0])) / max(1, len(bdb)), 3)}
    # scan alignment against major BD TOPO roads
    major12 = bd[bd["IMPORTANCE"].isin(["1", "2"])].geometry.values
    rep["scan_alignment"] = {}
    for key, path in (("scan100", paths.scan100), ("scan250", paths.scan250)):
        if path:
            rep["scan_alignment"][key] = raster_alignment(path, major12)
    rep["seconds"] = round(time.perf_counter() - t0, 1)
    if out:
        Path(out).write_text(json.dumps(rep, indent=1, ensure_ascii=False, default=str))
    return rep

"""Perception from the scanned IGN maps (the brief encourages image input).

Two narrow tools, both checked against the vector data:

* :func:`builtup_polygons` - built-up areas from the map's built-up tint
  (palette classes measured on the 1:100 000 scan: 251 = built-up, 226 =
  built-up under relief shading, 232 = industrial and commercial zones);
* :func:`ocr_labels` and :func:`towns_from_labels` - place names and the
  population figure IGN prints under town names (thousands of inhabitants,
  decimal comma, e.g. ``VANNES`` / ``53,0``), read with an ONNX OCR model
  (RapidOCR, PP-OCR weights, ~15 MB, CPU).

The population figures are what the exercise needs ("towns of more than 10k
inhabitants") and are not in either shapefile.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

BUILTUP_CLASSES_100K = (251, 226, 232)


def _lut(src) -> np.ndarray:
    cm = src.colormap(1)
    lut = np.zeros((256, 3), np.uint8)
    for k, v in cm.items():
        lut[k] = v[:3]
    return lut


def builtup_polygons(tif: str, classes=BUILTUP_CLASSES_100K, factor: int = 4, min_share: float = 0.25,
                     close_m: float = 90.0, min_area_ha: float = 3.0):
    """Built-up polygons (EPSG:4326) from the map tint: block share, morphological closing, vectorisation."""
    import geopandas as gpd
    import rasterio
    from rasterio import features
    from rasterio.enums import Resampling
    from scipy import ndimage
    from shapely.geometry import shape

    with rasterio.open(tif) as src:
        a = src.read(1)
        h, w = a.shape[0] // factor * factor, a.shape[1] // factor * factor
        m = np.isin(a[:h, :w], classes).reshape(h // factor, factor, w // factor, factor).mean(axis=(1, 3))
        transform = src.transform * src.transform.scale(factor, factor)
        lat0 = (src.bounds.top + src.bounds.bottom) / 2
        px_m = abs(transform.a) * 111_320 * math.cos(math.radians(lat0))
    mask = m >= min_share
    r = max(1, int(round(close_m / px_m)))
    st = ndimage.iterate_structure(ndimage.generate_binary_structure(2, 1), r)
    mask = ndimage.binary_closing(mask, structure=st)
    mask = ndimage.binary_fill_holes(mask)
    polys = [shape(g) for g, v in features.shapes(mask.astype(np.uint8), mask=mask, transform=transform) if v == 1]
    gdf = gpd.GeoDataFrame(geometry=polys, crs="EPSG:4326")
    gdf["area_ha"] = gdf.to_crs("EPSG:2154").area / 1e4
    gdf = gdf[gdf.area_ha >= min_area_ha].reset_index(drop=True)
    return gdf


@dataclass
class Label:
    text: str
    conf: float
    lon: float
    lat: float
    x: float  # full-resolution pixel centre
    y: float
    h: float  # box height in pixels
    w: float


def ocr_labels(tif: str, tile: int = 1600, overlap: int = 200, cache: str | None = None, min_conf: float = 0.5) -> list[Label]:
    """OCR every tile of the scan (RapidOCR); labels de-duplicated across tile overlaps. Cached as JSON."""
    if cache and Path(cache).exists():
        return [Label(**d) for d in json.loads(Path(cache).read_text())]
    import rasterio
    from rapidocr_onnxruntime import RapidOCR

    ocr = RapidOCR()
    out: list[Label] = []
    with rasterio.open(tif) as src:
        lut = _lut(src)
        H, W = src.height, src.width
        step = tile - overlap
        for r0 in range(0, H, step):
            for c0 in range(0, W, step):
                win = rasterio.windows.Window(c0, r0, min(tile, W - c0), min(tile, H - r0))
                img = lut[src.read(1, window=win)]
                res, _ = ocr(img)
                for box, txt, conf in res or []:
                    conf = float(conf)
                    if conf < min_conf:
                        continue
                    b = np.asarray(box, float)
                    x, y = c0 + b[:, 0].mean(), r0 + b[:, 1].mean()
                    lon, lat = src.xy(y, x)
                    out.append(Label(txt, conf, float(lon), float(lat), float(x), float(y),
                                     float(b[:, 1].max() - b[:, 1].min()), float(b[:, 0].max() - b[:, 0].min())))
    # de-duplicate (same text within 20 px)
    out.sort(key=lambda l: -l.conf)
    kept: list[Label] = []
    for l in out:
        if not any(k.text == l.text and abs(k.x - l.x) < 20 and abs(k.y - l.y) < 20 for k in kept):
            kept.append(l)
    if cache:
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        Path(cache).write_text(json.dumps([asdict(k) for k in kept], ensure_ascii=False))
    return kept


POP_RE = re.compile(r"^(\d{1,3})[,.](\d)$")
NAME_RE = re.compile(r"^[A-ZÀ-Ý'][A-ZÀ-Ý' .\-]{2,}$")


@dataclass
class Town:
    name: str
    population: int
    lon: float
    lat: float
    conf: float


def towns_from_labels(labels: list[Label], max_dx_px: float = 80, max_dy_factor: float = 1.9) -> list[Town]:
    """Pair each population figure (``53,0``) with the upper-case town name printed just above or below it.

    Measured on the 1:100 000 scan: the figure sits 25-45 px (about one name
    height) above or below the name, horizontally centred within ~60 px. The
    many other ``d,d`` figures on the map are road distances between
    junctions, which are not next to an upper-case name.
    """
    names = [l for l in labels if NAME_RE.match(l.text.strip()) and sum(c.isalpha() for c in l.text) >= 3]
    towns: list[Town] = []
    for p in labels:
        m = POP_RE.match(p.text.strip().replace(" ", ""))
        if not m:
            continue
        best, bd = None, math.inf
        for n in names:
            dy = abs(p.y - n.y)
            dx = abs(p.x - n.x)
            if 12 <= dy <= max_dy_factor * n.h and dx <= max(max_dx_px, n.w / 2):
                d = math.hypot(dx, dy)
                if d < bd:
                    best, bd = n, d
        if best is None:
            continue
        pop = int(round((int(m.group(1)) + int(m.group(2)) / 10) * 1000))
        towns.append(Town(best.text.strip(), pop, best.lon, best.lat, min(best.conf, p.conf)))
    uniq: dict[str, Town] = {}
    for t in sorted(towns, key=lambda t: -t.conf):
        uniq.setdefault(t.name, t)
    return sorted(uniq.values(), key=lambda t: -t.population)


def unresolved_towns(labels: list[Label], towns: list[Town], builtup, min_area_ha: float = 100.0,
                     min_height_px: float = 30.0) -> list[Label]:
    """Upper-case town-size labels inside a large built-up area for which no population figure was read."""
    import geopandas as gpd
    from shapely.geometry import Point

    big = builtup[builtup.area_ha >= min_area_ha]
    known = {t.name for t in towns}
    cand = [l for l in labels if NAME_RE.match(l.text.strip()) and l.h >= min_height_px and len(l.text.strip()) >= 4
            and l.text.strip() not in known]
    if not cand or big.empty:
        return []
    pts = gpd.GeoDataFrame(geometry=[Point(l.lon, l.lat) for l in cand], crs="EPSG:4326")
    hit = gpd.sjoin(pts, big[["geometry"]], predicate="within", how="inner").index.unique()
    out, seen = [], set()
    for i in sorted(hit):
        k = cand[i].text.strip()
        if k not in seen:
            seen.add(k)
            out.append(cand[i])
    return out


def town_areas(builtup, towns: list[Town], min_population: int, search_m: float = 1500.0):
    """Built-up polygons belonging to towns with at least ``min_population`` inhabitants."""
    import geopandas as gpd
    from shapely.geometry import Point

    bu = builtup.to_crs("EPSG:2154")
    pts = gpd.GeoSeries([Point(t.lon, t.lat) for t in towns], crs="EPSG:4326").to_crs("EPSG:2154")
    rows = []
    for t, p in zip(towns, pts):
        if t.population < min_population:
            continue
        d = bu.distance(p)
        near = bu[d <= search_m]
        if near.empty:
            continue
        # the largest polygon close to the label is the town's agglomeration
        idx = near.area.idxmax()
        rows.append({"town": t.name, "population": t.population, "geometry": bu.geometry[idx]})
    if not rows:
        return gpd.GeoDataFrame(columns=["town", "population", "geometry"], geometry="geometry", crs="EPSG:4326")
    g = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:2154")
    g = g.dissolve(by=g.geometry.apply(lambda x: x.wkb), aggfunc={"town": lambda s: " / ".join(sorted(set(s))), "population": "max"})
    return g.reset_index(drop=True).to_crs("EPSG:4326")


# ---------------------------------------------------------------------------
# exercise sketch: georeference the organisers' illustration and read its zones
# ---------------------------------------------------------------------------

def _fold(s: str) -> str:
    import unicodedata

    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]", "", s)


@dataclass
class SketchGeoref:
    rotation: int
    scale: float
    matches: list[tuple[str, str, float, float, float, float]]  # sketch text, map text, px, py, X, Y (L93)
    inliers: int
    median_residual_m: float
    affine: list[float]  # X = a*px + b*py + c ; Y = d*px + e*py + f

    def to_l93(self, px: np.ndarray, py: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        a, b, c, d, e, f = self.affine
        return a * px + b * py + c, d * px + e * py + f


def _sketch_image(path: str, rotation: int, scale: float):
    from PIL import Image

    im = Image.open(path).convert("RGB")
    if rotation:
        im = im.rotate(rotation, expand=True)
    if scale != 1:
        im = im.resize((int(im.width * scale), int(im.height * scale)))
    return np.asarray(im)


def georeference_sketch(path: str, labels: list[Label], scale: float = 2.0, seed: int = 0) -> SketchGeoref:
    """Match place names read on the sketch with names read on the IGN scan; fit an affine map with RANSAC.

    The sketch rotation (0, 90, -90, 180) is the one with the most confident OCR.
    """
    import difflib

    from rapidocr_onnxruntime import RapidOCR
    from pyproj import Transformer

    ocr = RapidOCR()
    best = None
    for rot in (0, 90, -90, 180):
        res, _ = ocr(_sketch_image(path, rot, scale))
        score = sum(float(c) for _, _, c in res or [])
        if best is None or score > best[0]:
            best = (score, rot, res)
    _, rot, res = best
    ref: dict[str, list[Label]] = {}
    for l in labels:
        k = _fold(l.text)
        if len(k) >= 4:
            ref.setdefault(k, []).append(l)
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    pairs = []
    for box, txt, conf in res:
        k = _fold(txt)
        k = k[:-1] if k.endswith("o") and k[:-1] in ref else k  # town marker read as a trailing 'o'
        if len(k) < 4:
            continue
        hit = difflib.get_close_matches(k, list(ref), n=1, cutoff=0.85)
        if not hit:
            continue
        b = np.asarray(box, float)
        for l in ref[hit[0]]:
            X, Y = tr.transform(l.lon, l.lat)
            pairs.append((txt, l.text, b[:, 0].mean(), b[:, 1].mean(), X, Y))
    P = np.array([[p[2], p[3], 1.0] for p in pairs])
    XY = np.array([[p[4], p[5]] for p in pairs])
    rng = np.random.default_rng(seed)
    best_in, best_A = None, None
    for _ in range(3000):
        idx = rng.choice(len(pairs), 3, replace=False)
        try:
            A = np.linalg.solve(P[idx], XY[idx])
        except np.linalg.LinAlgError:
            continue
        r = np.linalg.norm(P @ A - XY, axis=1)
        inl = r < 2500
        if best_in is None or inl.sum() > best_in.sum():
            best_in, best_A = inl, A
    A, *_ = np.linalg.lstsq(P[best_in], XY[best_in], rcond=None)
    r = np.linalg.norm(P[best_in] @ A - XY[best_in], axis=1)
    return SketchGeoref(rot, scale, [pairs[i] for i in np.flatnonzero(best_in)], int(best_in.sum()), float(np.median(r)),
                        [A[0, 0], A[1, 0], A[2, 0], A[0, 1], A[1, 1], A[2, 1]])


def sketch_zones(path: str, geo: SketchGeoref, n: int = 2, min_px: int = 2000):
    """The translucent blue areas drawn on the sketch, as polygons (EPSG:4326), largest first, named by position."""
    import geopandas as gpd
    from scipy import ndimage
    from shapely.geometry import MultiPoint

    a = _sketch_image(path, geo.rotation, geo.scale).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    m = (b - r > 60) & (b - g > 30) & (b > 150)
    m = ndimage.binary_closing(m, iterations=int(4 * geo.scale))
    lab, k = ndimage.label(m)
    sizes = ndimage.sum(m, lab, range(1, k + 1))
    order = [i + 1 for i in np.argsort(-sizes)[:n] if sizes[i] >= min_px]
    polys = []
    for i in order:
        ys, xs = np.nonzero(lab == i)
        X, Y = geo.to_l93(xs.astype(float), ys.astype(float))
        hull = MultiPoint(np.column_stack([X, Y])[:: max(1, len(X) // 20000)]).convex_hull
        polys.append(hull)
    gdf = gpd.GeoDataFrame({"px": [int(sizes[i - 1]) for i in order]}, geometry=polys, crs="EPSG:2154")
    gdf["area_km2"] = gdf.area / 1e6
    cy = gdf.centroid.y
    gdf["zone"] = ["North" if y == cy.max() else "South" if y == cy.min() else f"Zone {j}" for j, y in enumerate(cy)]
    return gdf.to_crs("EPSG:4326")


def reread_populations(tif: str, labels: list[Label], towns: list[Town], min_height_px: float = 30.0,
                       upscale: int = 2) -> tuple[list[Town], list[str]]:
    """Second OCR pass, upscaled, around every town-size upper-case name that got no population figure.

    Returns the completed town list and the names still without a figure.
    """
    import rasterio
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR

    known = {t.name for t in towns}
    todo = [l for l in labels if NAME_RE.match(l.text.strip()) and l.h >= min_height_px and len(l.text.strip()) >= 4
            and l.text.strip() not in known]
    seen: set[str] = set()
    ocr = RapidOCR()
    added, missing = [], []
    with rasterio.open(tif) as src:
        lut = _lut(src)
        for l in sorted(todo, key=lambda l: -l.conf):
            name = l.text.strip()
            if name in seen:
                continue
            seen.add(name)
            hw, hh = max(160, l.w), 2.2 * l.h
            win = rasterio.windows.Window(int(l.x - hw), int(l.y - hh), int(2 * hw), int(2 * hh))
            img = Image.fromarray(lut[src.read(1, window=win, boundless=True, fill_value=255)])
            img = img.resize((img.width * upscale, img.height * upscale), Image.LANCZOS)
            res, _ = ocr(np.asarray(img))
            sub = []
            for box, txt, conf in res or []:
                b = np.asarray(box, float) / upscale
                x, y = win.col_off + b[:, 0].mean(), win.row_off + b[:, 1].mean()
                lon, lat = src.xy(y, x)
                sub.append(Label(txt, float(conf), float(lon), float(lat), float(x), float(y),
                                 float(b[:, 1].max() - b[:, 1].min()), float(b[:, 0].max() - b[:, 0].min())))
            found = [t for t in towns_from_labels(sub + [l]) if t.name == name]
            if found:
                added.append(found[0])
            else:
                missing.append(name)
    return sorted(towns + added, key=lambda t: -t.population), missing


def places_from_labels(labels: list[Label], names: list[str]) -> dict[str, tuple[float, float]]:
    """Gazetteer seed from the scan: for each wanted name, the position of its largest label (town names are set larger)."""
    out = {}
    for name in names:
        k = _fold(name)
        cand = [l for l in labels if _fold(l.text) == k]
        if cand:
            best = max(cand, key=lambda l: (l.h, l.conf))
            out[name] = (best.lon, best.lat)
    return out

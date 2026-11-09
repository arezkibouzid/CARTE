"""S0 from the image: the road network read off the scanned IGN maps (1:100 000 or 1:250 000).

The brief encourages "focusing on the image input and keeping the shapefiles for sanity checks". This module
builds the canonical road graph of :mod:`coa_net.graph` from a scanned map alone; the BD TOPO and OSM shapefiles
are only used afterwards, by :mod:`coa_net.scancheck`, to measure how good the extracted network is.

Method (all classical image processing, CPU, no training data, every step measurable):

1. **Symbol classes from colour.** IGN draws every road as a *cased line*: two thin grey edges (the casing)
   around a fill whose colour is the road class: red (main roads), orange (departmental roads), white
   (minor roads), white with a *dashed* casing (tracks). Each pixel's RGB (the palette is read, not
   hard-coded) is put into one of: casing grey, red fill, orange fill, water, text black, other.
2. **Road body.** Casing and coloured fill are joined by a small morphological closing, which fills the white
   inside of minor roads and bridges the gaps of dashed casings. Thick blobs (built-up hatching, text, area
   tints) are removed with an opening at more than the widest road symbol.
3. **Centre lines.** The body is thinned to a one-pixel skeleton (tiles with overlap, so the whole map fits in
   memory), short spurs are pruned.
4. **Graph.** Junction pixels (three or more skeleton neighbours) become nodes, the pixel chains between them
   become polylines, converted to lon/lat with the GeoTIFF's transform and simplified (Douglas-Peucker, one
   pixel).
5. **Gaps.** Labels printed over roads cut the skeleton. A dead end is joined to the nearest line ahead of it
   (within ``gap_m`` and ``gap_deg``); every such join is flagged ``scan_gap`` so the plan can show it.
6. **Attributes from the symbol.** For each polyline: fill colour along the centre line -> road class; casing
   continuity -> paved road or track; symbol width; water ink on both sides of the road -> bridge (split out
   as its own short section). Everything else (real width, load class, slope) is unknown and stays ``None``,
   which the requirement layer prices or blocks like any other unknown.

The output is the same ``(polyline, attributes, direction)`` stream that :func:`coa_net.realdata.build_graph`
takes from the shapefile loaders, so the rest of COA-Net (corridors, requirements, exercise, plan, incidents)
runs unchanged on an image-derived network. Every road is two-way (one-way arrows are not read).
"""

from __future__ import annotations

import colorsys
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

import numpy as np

log = logging.getLogger(__name__)

SCAN_LOADER_VERSION = 6

# symbol classes (values of the class raster)
C_OTHER, C_CASING, C_RED, C_ORANGE, C_WATER, C_BLACK, C_WHITE, C_CASING_LIGHT, C_BLUE = 0, 1, 2, 3, 4, 5, 6, 7, 8
CLASS_NAMES = {C_OTHER: "other", C_CASING: "casing (dark grey)", C_CASING_LIGHT: "casing (mid grey)", C_BLUE: "blue fill",
               C_RED: "red fill", C_ORANGE: "orange fill", C_WATER: "water (light blue)", C_BLACK: "text black", C_WHITE: "white"}
FILLS = (C_BLUE, C_RED, C_ORANGE)
CASINGS = (C_CASING, C_CASING_LIGHT)

# map symbol -> canonical road class (IGN 1:100 000 legend). The scan gives the class of the symbol:
# blue = expressway / dual carriageway, red = main road, orange = departmental road of regional importance,
# white = other paved road, dashed = track. Checked against BD TOPO's IMPORTANCE in the validation report
# (symbol x class table), never read from it. The white symbol has one width for BD TOPO importance 4 and 5
# (measured: median 4-5 px for both), so the image cannot rank them: white roads are "unclassified" and an
# exercise on the image network searches roads of rank 5 and better.
SYMBOL_HIGHWAY = {"blue": "trunk", "red": "primary", "orange": "secondary", "white": "unclassified", "dashed": "track"}


@dataclass
class ScanParams:
    """Thresholds, in pixels of the scan unless stated. ``for_scan`` sets them from the pixel size."""

    channel_px: int = 4        # a white road's inside: casing at most this far on both sides
    close_px: int = 2          # closing radius: joins the fill to its junctions
    dash_px: int = 7           # line closing that joins the dashes of a track
    wide_px: int = 7           # opening radius: blobs this thick and large are areas, not roads
    wide_area_px: int = 600    # ... removed when their thick core is larger than this (a junction core is smaller)
    text_px: int = 2           # grey rims of black lettering, this far from black ink, are not casing
    spur_m: float = 60.0       # dead-end skeleton branches shorter than this are thinning artefacts
    merge_m: float = 60.0      # two junctions joined by a stub shorter than this are one crossroads
    gap_m: float = 120.0       # a dead end is joined to a line ahead of it within this distance
    gap_deg: float = 40.0      # ... and within this angle of the dead end's heading
    snap_m: float = 40.0       # otherwise to the nearest line in any direction within this distance
    min_component_m: float = 400.0  # isolated pieces shorter than this are dropped (labels, symbols)
    tile: int = 2048
    overlap: int = 48
    dash_ratio: float = 0.5    # white symbol whose probes meet a dark casing less often than this -> track
    simplify_px: float = 0.3
    smooth_px: int = 7         # moving-average window along the centre line (pixel staircase -> smooth bends)
    bridge_probe_px: tuple = (2, 7)  # water looked for this far on each side of the centre line
    river_px: int = 5          # water this wide on both sides is a river (bridge); thinner is a stream (culvert)

    @classmethod
    def for_scan(cls, px_m: float) -> "ScanParams":
        """1:100 000 scan: ~6-9 m pixels; 1:250 000: ~15-22 m pixels (symbols are about as wide in pixels)."""
        if px_m > 12:  # 1:250 000
            # the 1:250 000 draws minor roads with a mid grey casing and no tracks: no dashed class
            return cls(channel_px=3, close_px=2, dash_px=5, wide_px=5, spur_m=120.0, gap_m=250.0, min_component_m=1500.0,
                       bridge_probe_px=(2, 5), dash_ratio=0.0, river_px=2)
        return cls()


@dataclass
class ScanReport:
    """What the image loader saw and did (measured), shown in the validation report."""

    path: str
    width: int = 0
    height: int = 0
    crs: str = ""
    px_m: tuple = ()
    params: dict = field(default_factory=dict)
    palette_classes: dict = field(default_factory=dict)
    body_share: float = 0.0
    skeleton_px: int = 0
    spurs_pruned: int = 0
    polylines: int = 0
    gaps_joined: int = 0
    gaps_left: int = 0
    components_dropped: int = 0
    grade_separations: int = 0
    junctions_merged: int = 0
    water_lines_dropped: int = 0
    bridges: int = 0
    km_by_symbol: dict = field(default_factory=dict)
    seconds: dict = field(default_factory=dict)

    def markdown(self) -> str:
        rows = [("scan", self.path), ("size (px)", f"{self.width} x {self.height}"), ("CRS", self.crs),
                ("pixel (m, x / y)", " / ".join(f"{p:.1f}" for p in self.px_m)),
                ("road body share of the map", f"{self.body_share:.1%}"), ("skeleton pixels", self.skeleton_px),
                ("spurs pruned", self.spurs_pruned), ("junction pairs merged (crossroads)", self.junctions_merged), ("polylines", self.polylines),
                ("label gaps joined / dead ends left", f"{self.gaps_joined} / {self.gaps_left}"),
                ("small pieces dropped", self.components_dropped), ("blue lines on water dropped", self.water_lines_dropped),
                ("roads crossing an expressway kept apart (flyover or underpass)", self.grade_separations), ("bridges (water on both sides)", self.bridges),
                ("km by symbol", ", ".join(f"{k} {v:.0f}" for k, v in self.km_by_symbol.items())),
                ("time (s)", ", ".join(f"{k} {v:.1f}" for k, v in self.seconds.items()))]
        return "| item | value |\n| --- | --- |\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)


# ---------------------------------------------------------------------------
# 1. symbol classes from colour
# ---------------------------------------------------------------------------

def classify_rgb(rgb: np.ndarray) -> np.ndarray:
    """Symbol class of each RGB colour (``rgb``: N x 3 uint8). Rules on hue, saturation and value, so that the
    same rules read both scales and any palette order."""
    out = np.zeros(len(rgb), np.uint8)
    for i, (r, g, b) in enumerate(np.asarray(rgb, float) / 255):
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if v < 0.22:
            out[i] = C_BLACK
        elif s < 0.12 and v <= 0.72:
            # roads have a dark grey casing, tracks a mid grey dashed one
            out[i] = C_CASING if v < 0.5 else C_CASING_LIGHT
        elif s > 0.5 and v > 0.6 and (h <= 0.045 or h >= 0.96):
            out[i] = C_RED
        elif s > 0.55 and v > 0.6 and 0.075 <= h <= 0.14:
            out[i] = C_ORANGE
        elif 0.5 <= h <= 0.62 and s > 0.7 and v < 0.8:
            out[i] = C_BLUE  # expressway fill (dual carriageways); water names share it, they are too thin for a road body
        elif 0.5 <= h <= 0.62 and s > 0.2 and v > 0.75:
            out[i] = C_WATER  # rivers, lakes, streams: light blue
        elif s < 0.04 and v > 0.96:
            out[i] = C_WHITE
    return out


def palette_lut(src) -> np.ndarray:
    """Class of each palette index (palette GeoTIFF) as a 256-entry lookup table."""
    try:
        cm = src.colormap(1)
    except ValueError:
        cm = None
    if cm is None:
        raise ValueError("the scan has no colour palette; give an RGB scan through classify_rgb per pixel")
    rgb = np.zeros((256, 3), np.uint8)
    for k, v in cm.items():
        rgb[k] = v[:3]
    return classify_rgb(rgb)


# ---------------------------------------------------------------------------
# 2-3. road body and skeleton, per tile
# ---------------------------------------------------------------------------

def _disk(r: int) -> np.ndarray:
    y, x = np.ogrid[-r:r + 1, -r:r + 1]
    return (x * x + y * y <= r * r + r).astype(np.uint8)


def _shift(m: np.ndarray, dr: int, dc: int) -> np.ndarray:
    out = np.zeros_like(m)
    H, W = m.shape
    out[max(dr, 0):H + min(dr, 0), max(dc, 0):W + min(dc, 0)] = m[max(-dr, 0):H + min(-dr, 0), max(-dc, 0):W + min(-dc, 0)]
    return out


_DIRS = ((0, 1), (1, 0), (1, 1), (1, -1))


def road_body(cls: np.ndarray, p: ScanParams) -> np.ndarray:
    """Binary road body of a class tile.

    * coloured roads: their red or orange fill;
    * white roads and tracks: the *channel* between two casing lines - a white pixel with casing at most
      ``channel_px`` away on both opposite sides, in one of four directions. A built-up block's outline
      encloses a beige tint, not white, so blocks do not make channels; lettering is excluded with its rim.
    A small closing then joins the dashes of a track and the fill to its junctions. Large thick blobs are
    removed (they are areas, not roads)."""
    import cv2

    cas = np.isin(cls, CASINGS)
    black = (cls == C_BLACK).astype(np.uint8)
    if black.any():  # lettering: its anti-aliased grey rim looks like casing
        cas &= ~(cv2.dilate(black, _disk(p.text_px)) > 0)
    white = cls == C_WHITE
    chan = np.zeros_like(cas)
    for dr, dc in _DIRS:
        left = np.zeros_like(cas)
        right = np.zeros_like(cas)
        for k in range(1, p.channel_px + 1):
            left |= _shift(cas, k * dr, k * dc)
            right |= _shift(cas, -k * dr, -k * dc)
        chan |= left & right
    fills = np.isin(cls, FILLS)
    body = (chan & white) | fills
    body = body.astype(np.uint8)
    # dashes of a track: closing with a short line in four directions joins them along the track
    L = p.dash_px
    joined = body.copy()
    for se in (np.ones((1, L), np.uint8), np.ones((L, 1), np.uint8), np.eye(L, dtype=np.uint8), np.fliplr(np.eye(L, dtype=np.uint8))):
        joined |= cv2.morphologyEx(body, cv2.MORPH_CLOSE, se)
    body = cv2.morphologyEx(joined, cv2.MORPH_CLOSE, _disk(p.close_px))
    thick = cv2.morphologyEx(body, cv2.MORPH_OPEN, _disk(p.wide_px))
    if thick.any():
        n, lab, stats, _ = cv2.connectedComponentsWithStats(thick, connectivity=8)
        big = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] > p.wide_area_px)
        big = big[big > 0]
        if len(big):
            # coloured fills are always roads (an expressway is wider than the opening): only the rest is removed
            body[(cv2.dilate(np.isin(lab, big).astype(np.uint8), _disk(p.close_px)) > 0) & ~fills] = 0
    return body


def skeleton_tiles(idx_reader, shape: tuple, lut: np.ndarray, p: ScanParams, progress=None):
    """Road body tile by tile (local operations, tiles overlap by more than any kernel), then one skeleton of
    the whole map so that no road is cut at a tile edge.

    Returns (skeleton bool, class raster, symbol width in pixels on the skeleton, body pixel count)."""
    import cv2
    from skimage.morphology import skeletonize

    H, W = shape
    body_all = np.zeros((H, W), np.uint8)
    classes = np.zeros((H, W), np.uint8)
    T, O = p.tile, p.overlap
    for r0 in range(0, H, T):
        for c0 in range(0, W, T):
            ra, ca = max(r0 - O, 0), max(c0 - O, 0)
            rb, cb = min(r0 + T + O, H), min(c0 + T + O, W)
            cls = lut[idx_reader(ra, ca, rb - ra, cb - ca)]
            body = road_body(cls, p)
            ir, ic = r0 - ra, c0 - ca
            hr, hc = min(T, H - r0), min(T, W - c0)
            body_all[r0:r0 + hr, c0:c0 + hc] = body[ir:ir + hr, ic:ic + hc]
            classes[r0:r0 + hr, c0:c0 + hc] = cls[ir:ir + hr, ic:ic + hc]
            if progress:
                progress(r0, c0)
    body_px = int(body_all.sum())
    skel = skeletonize(body_all > 0)
    dt = cv2.distanceTransform(body_all, cv2.DIST_L2, 3)
    del body_all
    widths = np.zeros((H, W), np.uint8)
    widths[skel] = np.clip(np.rint(2 * dt[skel]), 0, 255).astype(np.uint8)
    return skel, classes, widths, body_px


# ---------------------------------------------------------------------------
# 4. skeleton -> pixel polylines
# ---------------------------------------------------------------------------

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def _neighbour_count(skel: np.ndarray) -> np.ndarray:
    import cv2

    k = np.ones((3, 3), np.float32)
    k[1, 1] = 0
    n = cv2.filter2D(skel.astype(np.uint8), cv2.CV_8U, k, borderType=cv2.BORDER_CONSTANT)
    return np.where(skel, n, 0).astype(np.uint8)


def trace_skeleton(skel: np.ndarray) -> tuple[list[np.ndarray], np.ndarray]:
    """Pixel chains between junctions. Returns (chains as K x 2 int arrays of (row, col), junction label raster).

    A junction is a connected cluster of pixels with three or more neighbours; each chain starts and ends
    on a junction pixel or a dead end, so chains sharing a junction cluster meet at its centroid later."""
    from scipy import ndimage

    nb = _neighbour_count(skel)
    junction = nb >= 3
    jlab, nj = ndimage.label(junction, structure=np.ones((3, 3)))
    body = skel & ~junction
    lab, n = ndimage.label(body, structure=np.ones((3, 3)))
    objs = ndimage.find_objects(lab)
    H, W = skel.shape
    chains: list[np.ndarray] = []
    for k, sl in enumerate(objs, start=1):
        if sl is None:
            continue
        r0, c0 = sl[0].start, sl[1].start
        sub = lab[sl] == k
        pts = np.argwhere(sub)
        if len(pts) == 0:
            continue
        pset = {(int(r) + r0, int(c) + c0) for r, c in pts}

        def nbrs(q):
            return [(q[0] + dr, q[1] + dc) for dr, dc in _NB if (q[0] + dr, q[1] + dc) in pset]

        ends = [q for q in pset if len(nbrs(q)) <= 1]
        start = min(ends) if ends else min(pset)  # a closed loop starts anywhere
        path, seen, cur = [start], {start}, start
        while True:
            nx_ = [q for q in nbrs(cur) if q not in seen]
            if not nx_:
                break
            # prefer 4-connected steps so that the chain does not skip a pixel at a diagonal
            nx_.sort(key=lambda q: abs(q[0] - cur[0]) + abs(q[1] - cur[1]))
            cur = nx_[0]
            seen.add(cur)
            path.append(cur)
        if len(path) < len(pset):  # side branches inside a chain (rare): keep the longest walk, drop the rest
            pass
        arr = np.asarray(path, np.int32)
        # attach the junction clusters touching each end
        ext = []
        for end in (arr[0], arr[-1]):
            js = {int(jlab[end[0] + dr, end[1] + dc]) for dr, dc in _NB
                  if 0 <= end[0] + dr < H and 0 <= end[1] + dc < W and jlab[end[0] + dr, end[1] + dc] > 0}
            ext.append(min(js) if js else 0)
        chains.append(np.column_stack([arr, np.zeros(len(arr), np.int32)]) if False else arr)
        chains[-1] = (arr, ext[0], ext[1])
    # junction clusters with no chain at all (two junction pixels touching) still connect their neighbours:
    # chains adjacent to the same cluster meet at its centroid
    return chains, jlab


def junction_centroids(jlab: np.ndarray) -> dict[int, tuple[float, float]]:
    from scipy import ndimage

    n = int(jlab.max())
    if n == 0:
        return {}
    cents = ndimage.center_of_mass(np.ones_like(jlab, np.uint8), jlab, index=np.arange(1, n + 1))
    return {i + 1: (float(r), float(c)) for i, (r, c) in enumerate(cents)}


def merge_close_junctions(chains: list, cent: dict, max_px: float) -> tuple[list, dict, int]:
    """Contract chains shorter than ``max_px`` that join two junctions; the merged node sits at the mean of
    the junction centroids. Returns (chains, centroids, merges)."""
    parent = {j: j for j in cent}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    n = 0
    keep = []
    for arr, ja, jb in chains:
        if ja and jb and ja != jb and len(arr) < max_px:
            ra, rb = find(ja), find(jb)
            if ra != rb:
                parent[rb] = ra
                n += 1
                continue
        keep.append((arr, ja, jb))
    groups: dict = {}
    for j in cent:
        groups.setdefault(find(j), []).append(cent[j])
    new_cent = {r: tuple(np.mean(np.asarray(v), axis=0)) for r, v in groups.items()}
    out = []
    for arr, ja, jb in keep:
        ja, jb = (find(ja) if ja else 0), (find(jb) if jb else 0)
        if ja and ja == jb and len(arr) < max_px:
            continue  # a tiny loop left inside a merged crossroads
        out.append((arr, ja, jb))
    return out, new_cent, n


# ---------------------------------------------------------------------------
# 6. attributes from the symbol
# ---------------------------------------------------------------------------

def chain_symbol(path: np.ndarray, classes: np.ndarray, widths: np.ndarray, p: ScanParams) -> dict:
    """Read the symbol along a pixel chain: fill colour on the centre line, casing kind on both sides, symbol
    width (twice the body's distance transform on the centre line), light-blue water on both sides (bridges)."""
    H, W = classes.shape
    n = len(path)
    centre = classes[path[:, 0], path[:, 1]]
    share = {c: float(np.mean(centre == c)) for c in FILLS}
    w = widths[path[:, 0], path[:, 1]].astype(float)
    w = w[w > 0]
    # tangents from the points 4 pixels before and after, normals by rotation
    i0, i1 = np.clip(np.arange(n) - 4, 0, n - 1), np.clip(np.arange(n) + 4, 0, n - 1)
    t = (path[i1] - path[i0]).astype(float)
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    nrm = np.column_stack([-t[:, 1], t[:, 0]])
    lo, hi = p.bridge_probe_px
    ks = np.arange(1, hi + p.river_px + 1, dtype=float)
    dark = np.zeros(n, bool)
    water_side, far_water = [], []
    for sgn in (1.0, -1.0):
        q = np.rint(path[:, None, :] + sgn * ks[None, :, None] * nrm[:, None, :]).astype(int)
        inside = (q[..., 0] >= 0) & (q[..., 0] < H) & (q[..., 1] >= 0) & (q[..., 1] < W)
        cl = np.where(inside, classes[np.clip(q[..., 0], 0, H - 1), np.clip(q[..., 1], 0, W - 1)], C_OTHER)
        dark |= (cl[:, :hi] == C_CASING).any(axis=1)
        water_side.append((cl[:, lo - 1:] == C_WATER).sum(axis=1))
        far_water.append((cl[:, hi - 1:] == C_WATER).sum(axis=1) >= 3)  # well beyond the symbol's anti-aliased edge
    # a river (water at least river_px wide on both sides) makes a bridge; a stream line (1-2 px) a culvert
    water_both = (water_side[0] >= p.river_px) & (water_side[1] >= p.river_px)
    on_water = float((far_water[0] & far_water[1]).mean()) if n else 0.0
    stream = bool(((water_side[0] >= 1) & (water_side[1] >= 1)).any())
    if share[C_BLUE] >= 0.35:
        # the expressway fill's anti-aliased edge is light blue like water: no bridge is read on a blue road
        water_both[:] = False
    if water_both.any():  # widen each crossing by a few pixels: the river line is thinner than the bridge
        k = np.ones(5, bool)
        water_both = np.convolve(water_both, k, mode="same") > 0
    cas = float(dark.mean()) if n else 0.0
    width_px = float(np.median(w)) if len(w) else None
    if share[C_BLUE] >= 0.35:
        sym = "blue"
    elif share[C_RED] >= 0.35:
        sym = "red"
    elif share[C_ORANGE] >= 0.35:
        sym = "orange"
    elif cas < p.dash_ratio:
        sym = "dashed"
    else:
        sym = "white"
    return {"symbol": sym, "casing": cas, "width_px": width_px, "water_both": water_both, "stream": stream and not water_both.any(),
            "on_water": on_water}


# ---------------------------------------------------------------------------
# 5. gaps and assembly
# ---------------------------------------------------------------------------

def _pix_to_lonlat(transform, rc: np.ndarray) -> np.ndarray:
    rows, cols = rc[:, 0] + 0.5, rc[:, 1] + 0.5
    x = transform.c + cols * transform.a + rows * transform.b
    y = transform.f + cols * transform.d + rows * transform.e
    return np.column_stack([x, y])


def _smooth(rc: np.ndarray, win: int) -> np.ndarray:
    """Moving average along a pixel chain (ends fixed): removes the pixel staircase, which would otherwise read
    as a string of tight turns, while keeping bends longer than the window."""
    rc = np.asarray(rc, float)
    if win < 3 or len(rc) < win + 2:
        return rc
    k = np.ones(win) / win
    pad = win // 2
    ext = np.vstack([np.repeat(rc[:1], pad, 0), rc, np.repeat(rc[-1:], pad, 0)])
    out = np.column_stack([np.convolve(ext[:, 0], k, mode="valid"), np.convolve(ext[:, 1], k, mode="valid")])
    out[0], out[-1] = rc[0], rc[-1]
    return out


def _simplify(rc: np.ndarray, tol: float) -> np.ndarray:
    import shapely

    if len(rc) <= 2:
        return rc.astype(float)
    ls = shapely.LineString(rc.astype(float)).simplify(tol, preserve_topology=False)
    return np.asarray(ls.coords)


@dataclass
class Piece:
    rc: np.ndarray        # float (row, col) polyline, ends on junction centroids
    a: int                # end node key ("j<id>" or "e<id>")
    b: int
    sym: dict
    gap: bool = False


def extract(tif: str, params: ScanParams | None = None, window=None, progress=None) -> tuple[list, ScanReport]:
    """Read the road network off a georeferenced palette scan.

    Returns the segment stream ``[(lon/lat polyline, attributes, "both"), ...]`` for
    :func:`coa_net.realdata.build_graph` and a :class:`ScanReport`. ``window``: optional rasterio Window to
    process part of the map (tests)."""
    import rasterio
    from rasterio.windows import Window
    from scipy.spatial import cKDTree

    T: dict[str, float] = {}
    t0 = time.perf_counter()
    with rasterio.open(tif) as src:
        lut = palette_lut(src)
        win = window or Window(0, 0, src.width, src.height)
        transform = src.window_transform(win)
        H, W = int(win.height), int(win.width)
        lat0 = (src.bounds.top + src.bounds.bottom) / 2
        if src.crs and src.crs.is_geographic:
            px_x = abs(transform.a) * 111_320 * math.cos(math.radians(lat0))
            px_y = abs(transform.e) * 110_570
        else:
            px_x, px_y = abs(transform.a), abs(transform.e)
        p = params or ScanParams.for_scan((px_x + px_y) / 2)
        rep = ScanReport(path=str(tif), width=W, height=H, crs=str(src.crs), px_m=(px_x, px_y), params=asdict(p))
        rep.palette_classes = {CLASS_NAMES[k]: int(np.sum(lut == k)) for k in CLASS_NAMES}

        def reader(r, c, h, w):
            return src.read(1, window=Window(win.col_off + c, win.row_off + r, w, h))

        skel, classes, widths, body_px = skeleton_tiles(reader, (H, W), lut, p, progress)
    T["classes, body, skeleton"] = time.perf_counter() - t0
    rep.body_share = body_px / (H * W)
    px_m = (px_x + px_y) / 2

    # prune short spurs (one pass on chains: a dead-end chain shorter than spur_m hanging off a junction)
    t = time.perf_counter()
    chains, jlab = trace_skeleton(skel)
    spur_px = p.spur_m / px_m
    deg: dict[int, int] = {}
    for _, ja, jb in chains:
        for j in (ja, jb):
            if j:
                deg[j] = deg.get(j, 0) + 1
    keep = []
    for ch in chains:
        arr, ja, jb = ch
        dead = (ja == 0) != (jb == 0)
        if dead and len(arr) < spur_px and deg.get(ja or jb, 0) >= 3:
            rep.spurs_pruned += 1
            for r_, c_ in arr:
                skel[r_, c_] = False
            continue
        keep.append(ch)
    # re-trace after pruning so that junctions left with two branches become plain points
    chains, jlab = trace_skeleton(skel)
    rep.skeleton_px = int(skel.sum())
    cent = junction_centroids(jlab)
    # a crossroads thins into two T-junctions joined by a stub about one road width long: contract such stubs
    chains, cent, rep.junctions_merged = merge_close_junctions(chains, cent, p.merge_m / px_m)
    T["trace"] = time.perf_counter() - t

    # pieces with their symbol, bridges split out
    t = time.perf_counter()
    pieces: list[Piece] = []
    end_id = [0]

    def new_end():
        end_id[0] += 1
        return -end_id[0]

    for arr, ja, jb in chains:
        if len(arr) < 2 and not (ja and jb):
            continue
        sym = chain_symbol(arr, classes, widths, p)
        if sym["symbol"] == "blue" and sym["on_water"] > 0.5:
            rep.water_lines_dropped += 1  # a dark blue line drawn on the sea or a lake (limits, ferry lines): not a road
            continue
        pts = arr.astype(float)
        if ja:
            pts = np.vstack([cent[ja], pts])
        if jb:
            pts = np.vstack([pts, cent[jb]])
        a = ja if ja else new_end()
        b = jb if jb else new_end()
        wb = sym.pop("water_both")
        wb = np.concatenate([[False] if ja else [], wb, [False] if jb else []]).astype(bool)
        # split bridge runs out as their own sections
        if wb.any() and len(pts) >= 4:
            cuts = np.flatnonzero(np.diff(wb.astype(int))) + 1
            bounds = [0] + list(cuts) + [len(pts)]
            prev_end = a
            for s, e in zip(bounds[:-1], bounds[1:]):
                seg = pts[max(s - 1, 0):e] if s > 0 else pts[s:e]
                if len(seg) < 2:
                    continue
                last = e == len(pts)
                end = b if last else new_end()
                ps = dict(sym, bridge=bool(wb[s]))
                pieces.append(Piece(seg, prev_end, end, ps))
                prev_end = end
                if ps["bridge"]:
                    rep.bridges += 1
            # glue the run ends: consecutive pieces share their boundary point exactly (seg starts at e-1)
        else:
            pieces.append(Piece(pts, a, b, dict(sym, bridge=False)))
    T["symbols"] = time.perf_counter() - t

    # an expressway has no level crossing: a road drawn across it is a flyover or an underpass
    rep.grade_separations = separate_expressway_crossings(pieces)
    # small isolated pieces (lettering, symbols, hatching) are dropped before gaps are joined
    pieces, rep.components_dropped = drop_small_components(pieces, p.min_component_m / px_m)

    # 5. gaps: join each dead end to the nearest line ahead of it
    t = time.perf_counter()
    end_deg: dict[int, int] = {}
    for pc in pieces:
        end_deg[pc.a] = end_deg.get(pc.a, 0) + 1
        end_deg[pc.b] = end_deg.get(pc.b, 0) + 1
    verts, owner = [], []
    for i, pc in enumerate(pieces):
        verts.append(pc.rc)
        owner.append(np.full(len(pc.rc), i))
    V = np.vstack(verts) if verts else np.zeros((0, 2))
    O = np.concatenate(owner) if owner else np.zeros(0, int)
    tree = cKDTree(V) if len(V) else None
    start = np.cumsum([0] + [len(pc.rc) for pc in pieces])
    gap_px, snap_px = p.gap_m / px_m, p.snap_m / px_m
    cos_max = math.cos(math.radians(p.gap_deg))
    cos_merge = math.cos(math.radians(45.0))
    new_pieces: list[Piece] = []
    splits: dict[int, set[int]] = {}
    for i, pc in enumerate(pieces):
        for end_key, pts, inner in ((pc.a, pc.rc, pc.rc[min(6, len(pc.rc) - 1)]), (pc.b, pc.rc[::-1], pc.rc[::-1][min(6, len(pc.rc) - 1)])):
            if end_key >= 0 or end_deg.get(end_key, 0) != 1:
                continue
            e = pts[0]
            d = e - inner
            nd = np.hypot(*d)
            if nd == 0:
                continue
            d = d / nd
            best = None
            src_blue = pc.sym["symbol"] == "blue"

            def merge_ok(j, d=d, src_blue=src_blue):
                # a road meets an expressway only on a slip road, at a shallow angle; a road broken where it
                # crosses the expressway (drawn over it) must not be joined to it
                k = O[j]
                if (pieces[k].sym["symbol"] == "blue") == src_blue:
                    return True
                lo_, hi_ = max(j - 3, int(start[k])), min(j + 3, int(start[k + 1]) - 1)
                tv = V[hi_] - V[lo_]
                nt = np.hypot(*tv)
                return nt > 0 and abs(float(np.dot(tv / nt, d))) >= cos_merge

            for j in tree.query_ball_point(e, gap_px):
                if O[j] == i:
                    continue
                v = V[j] - e
                dist = np.hypot(*v)
                if dist < 1:
                    continue
                if np.dot(v / dist, d) < cos_max or not merge_ok(j):
                    continue
                score = dist * (2 - np.dot(v / dist, d))
                if best is None or score < best[0]:
                    best = (score, j, dist)
            if best is None:
                # a road that stops just short of another (the junction's ink broke): snap in any direction
                for j in tree.query_ball_point(e, snap_px):
                    if O[j] == i or not merge_ok(j):
                        continue
                    dist = float(np.hypot(*(V[j] - e)))
                    if dist >= 1 and (best is None or dist < best[0]):
                        best = (dist, j, dist)
            if best is None:
                rep.gaps_left += 1
                continue
            j = best[1]
            k = O[j]
            local = j - int(start[k])
            splits.setdefault(int(k), set()).add(int(local))
            new_pieces.append(Piece(np.vstack([e, V[j]]), end_key, ("v", int(k), int(local)), dict(pc.sym, bridge=False), gap=True))
            end_deg[end_key] = 2
            rep.gaps_joined += 1
    T["gaps"] = time.perf_counter() - t

    # assemble lon/lat segments; splitting a piece at a joined vertex makes that vertex a node
    def ll(rc):
        return _pix_to_lonlat(transform, np.asarray(rc, float)[:, :2] - 0.5 + 0.5)

    node_pt: dict = {}

    def key_pt(key, rc_pt):
        if key not in node_pt:
            node_pt[key] = tuple(np.round(ll(np.asarray([rc_pt]))[0], 7))
        return node_pt[key]

    segs = []
    for i, pc in enumerate(pieces):
        cut = sorted(splits.get(i, set()) - {0, len(pc.rc) - 1})
        bounds = [0] + cut + [len(pc.rc) - 1]
        for s, e in zip(bounds[:-1], bounds[1:]):
            rc = pc.rc[s:e + 1]
            ka = pc.a if s == 0 else ("v", i, s)
            kb = pc.b if e == len(pc.rc) - 1 else ("v", i, e)
            segs.append((rc, ka, kb, pc.sym, False))
    for pc in new_pieces:
        segs.append((pc.rc, pc.a, pc.b, pc.sym, True))

    out = []
    km: dict[str, float] = {}
    for rc, ka, kb, sym, gap in segs:
        simp = _simplify(_smooth(rc, p.smooth_px), p.simplify_px) if len(rc) > 2 else rc
        coords = ll(simp)
        coords = [tuple(np.round(c, 7)) for c in coords]
        coords[0] = key_pt(ka, rc[0])
        coords[-1] = key_pt(kb, rc[-1])
        if len(coords) < 2 or coords[0] == coords[-1]:
            continue
        a = scan_attrs(sym, px_m, gap)
        out.append((coords, a, "both"))
        km[a["scan_symbol"]] = km.get(a["scan_symbol"], 0.0) + len(rc) * px_m / 1000
    rep.polylines = len(out)
    rep.km_by_symbol = dict(sorted(km.items(), key=lambda kv: -kv[1]))
    T["assemble"] = time.perf_counter() - t
    rep.seconds = {k: round(v, 1) for k, v in T.items()}
    return out, rep


def _arm(pc: "Piece", at_start: bool, k: int = 8) -> np.ndarray:
    pts = pc.rc if at_start else pc.rc[::-1]
    d = pts[min(k, len(pts) - 1)] - pts[0]
    n = np.hypot(*d)
    return d / n if n > 0 else d


def separate_expressway_crossings(pieces: list, straight_deg: float = 150.0) -> int:
    """At a junction with at least two blue (expressway) arms, every pair of other arms that runs straight
    through (angle above ``straight_deg``) is a road crossing the expressway on a bridge or in an underpass:
    the pair gets its own node (1 cm away), so a route cannot turn onto the expressway there. Other arms (slip
    roads, which join at an angle) stay on the junction. Also covers a road crossing both carriageways, whose
    two crossings the crossroads merge has made one junction with four blue arms."""
    arms: dict[int, list] = {}
    for i, pc in enumerate(pieces):
        if pc.a > 0:
            arms.setdefault(pc.a, []).append((i, True))
        if pc.b > 0:
            arms.setdefault(pc.b, []).append((i, False))
    cos_s = math.cos(math.radians(straight_deg))
    next_id = max(arms, default=0) + 1
    n = 0
    for j, lst in arms.items():
        blue = [(i, s) for i, s in lst if pieces[i].sym["symbol"] == "blue"]
        other = [(i, s) for i, s in lst if pieces[i].sym["symbol"] != "blue"]
        if len(blue) < 2 or len(other) < 2:
            continue
        vo = [_arm(pieces[i], s) for i, s in other]
        used: set[int] = set()
        pairs = sorted(((float(np.dot(vo[x], vo[y])), x, y) for x in range(len(other)) for y in range(x + 1, len(other))))
        for c, x, y in pairs:
            if c > cos_s or x in used or y in used:
                continue
            used |= {x, y}
            for i, s in (other[x], other[y]):
                pc = pieces[i]
                pc.rc = pc.rc.copy()
                if s:
                    pc.a = next_id
                    pc.rc[0] = pc.rc[0] + 0.01
                else:
                    pc.b = next_id
                    pc.rc[-1] = pc.rc[-1] + 0.01
            next_id += 1
            n += 1
    return n


def drop_small_components(pieces: list, min_px: float) -> tuple[list, int]:
    """Drop connected groups of pieces whose total length (pixels) is below ``min_px``."""
    parent: dict = {}

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for pc in pieces:
        parent[find(pc.a)] = find(pc.b)
    length: dict = {}
    for pc in pieces:
        r = find(pc.a)
        length[r] = length.get(r, 0.0) + float(np.hypot(*np.diff(pc.rc, axis=0).T).sum())
    small = {r for r, L in length.items() if L < min_px}
    return [pc for pc in pieces if find(pc.a) not in small], len(small)


def scan_attrs(sym: dict, px_m: float, gap: bool) -> dict:
    """Canonical edge attributes from what the symbol shows. Real width, load class, slope: unknown (None)."""
    s = sym["symbol"]
    hw = SYMBOL_HIGHWAY[s]
    return {
        "highway": hw,
        "paved": False if s == "dashed" else True,
        "bridge": bool(sym.get("bridge")),
        "tunnel": False,
        "name": "",
        "width_m": None,
        "scan_symbol": s,
        "scan_width_px": sym.get("width_px"),
        "scan_casing": round(float(sym.get("casing", 0.0)), 2),
        "scan_gap": gap,
        "scan_stream": bool(sym.get("stream")) and not sym.get("bridge"),  # crosses a stream line: culvert or small bridge
        "source_id": "scan",
    }


def load_scenario_scan(tif: str, params: ScanParams | None = None, window=None, simplify: bool = True):
    """Canonical graph from a scanned map, with a :class:`coa_net.realdata.LoadReport` like the shapefile loaders."""
    from .realdata import LoadReport, _finish, build_graph, keep_largest_scc

    t = time.perf_counter()
    segs, srep = extract(tif, params, window=window)
    rep = LoadReport("scanned map (image)", tif, features=len(segs), kept=len(segs), crs=srep.crs, encoding="-",
                     bounds=_bounds(segs))
    G = build_graph(segs, name=f"scan:{tif}", simplify=simplify)
    rep.largest_scc_share = keep_largest_scc(G)
    _finish(G, rep, t)
    rep.notes = [f"road network read from the image: {srep.polylines} polylines, {srep.gaps_joined} label gaps joined, "
                 f"{srep.bridges} bridges; every road two-way, width/load class/slope unknown"]
    G.graph["scan_report"] = asdict(srep)
    return G, rep, srep


def _bounds(segs) -> tuple:
    if not segs:
        return ()
    xy = np.vstack([np.asarray(c) for c, _, _ in segs])
    return (float(xy[:, 0].min()), float(xy[:, 1].min()), float(xy[:, 0].max()), float(xy[:, 1].max()))


SYMBOL_COLOURS = {"blue": "#1f5fbf", "red": "#d62728", "orange": "#ff9f1c", "white": "#6a3d9a", "dashed": "#2ca02c"}


def network_frame(G):
    """One row per undirected road of an image-derived graph (EPSG:4326), for GeoJSON and figures."""
    import geopandas as gpd
    import shapely

    seen, geoms, rows = set(), [], []
    for u, v, d in G.edges(data=True):
        k = (u, v) if str(u) <= str(v) else (v, u)
        if k in seen:
            continue
        seen.add(k)
        geoms.append(shapely.LineString(d["coords"]))
        rows.append({"symbol": d.get("scan_symbol"), "highway": d.get("highway"), "road_rank": d.get("road_rank"),
                     "bridge": bool(d.get("bridge")), "stream_crossing": bool(d.get("scan_stream")),
                     "label_gap": bool(d.get("scan_gap")), "length_m": round(d["length_m"], 1),
                     "two_way": G.has_edge(v, u)})
    return gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")


def write_network_outputs(G, tif: str, out) -> None:
    """network.geojson, scan_report.md and map_network.png (the network over the scan, coloured by symbol)."""
    from pathlib import Path

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    gdf = network_frame(G)
    gdf.to_file(out / "network.geojson", driver="GeoJSON")
    rep = G.graph.get("scan_report")
    if rep:
        (out / "scan_report.md").write_text("# Road network read from the scan\n\n" + ScanReport(**rep).markdown() + "\n", encoding="utf-8")
    render_network(G, tif, out / "map_network.png", gdf=gdf)


def render_network(G, tif: str, path, gdf=None, bounds=None, title: str | None = None, dpi: int = 110, ref_lines=None) -> None:
    """The network over the scan, coloured by symbol; ``ref_lines`` (a GeoDataFrame, e.g. BD TOPO roads) is drawn
    as thin black lines on top, for the sanity-check figures."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    gdf = network_frame(G) if gdf is None else gdf
    with rasterio.open(tif) as src:
        b = bounds or tuple(src.bounds)
        win = from_bounds(*b, src.transform)
        scale = max(1, int(max(win.width, win.height) // 3000))
        a = src.read(1, window=win, out_shape=(int(win.height // scale), int(win.width // scale)), resampling=Resampling.nearest)
        cm = src.colormap(1)
    lut = np.zeros((256, 3), np.uint8)
    for k, v in cm.items():
        lut[k] = v[:3]
    w_in = 12
    h_in = w_in * (b[3] - b[1]) / ((b[2] - b[0]) * math.cos(math.radians((b[1] + b[3]) / 2)))
    fig, ax = plt.subplots(figsize=(w_in, max(4, min(h_in, 16))))
    ax.imshow(lut[a], extent=(b[0], b[2], b[1], b[3]), alpha=0.45)
    k = 3.0 if bounds else 1.0  # thicker lines on a detail
    if bounds:
        import shapely

        gdf = gdf[gdf.intersects(shapely.box(*b))]
    for sym, col in SYMBOL_COLOURS.items():
        g = gdf[(gdf["symbol"] == sym) & ~gdf["label_gap"]]
        if len(g):
            g.plot(ax=ax, color=col, linewidth=k * (0.6 if sym in ("white", "dashed") else 1.0),
                   label=sym if bounds else f"{sym} ({g.length_m.sum() / 1000:.0f} km)")
    g = gdf[gdf["label_gap"]]
    if len(g):
        g.plot(ax=ax, color="magenta", linewidth=k * 1.2, label="joined across a label" + ("" if bounds else f" ({len(g)})"))
    g = gdf[gdf["bridge"]]
    if len(g):
        g.plot(ax=ax, color="cyan", linewidth=k * 2, label="bridge over a river" + ("" if bounds else f" ({len(g)})"))
    if ref_lines is not None and len(ref_lines):
        ref_lines.to_crs("EPSG:4326").plot(ax=ax, color="black", linewidth=0.4, linestyle=(0, (3, 2)), label="BD TOPO (check only)")
    ax.set_xlim(b[0], b[2])
    ax.set_ylim(b[1], b[3])
    ax.set_aspect(1 / math.cos(math.radians((b[1] + b[3]) / 2)))
    ax.legend(loc="lower left", fontsize=8, framealpha=0.9)
    ax.set_title(title or "Road network read from the scan (colour = map symbol)")
    plt.tight_layout()
    plt.savefig(path, dpi=dpi)
    plt.close(fig)

"""Learned corrector for the road network read from a scanned map.

The image reader (:mod:`coa_net.scanroads`) takes a few uncertain decisions by fixed rules: a label gap is
joined, a road drawn across another is a crossroads, a white road is rank 5, light blue on both sides is a
bridge. This module learns to correct those decisions from the shapefiles, which line up with the scan:

* **elements** - every road of the image network, every crossing where two roads run straight through the same
  junction, with features read only from the image and the image network (symbol, width, casing, lettering and
  water around it, length, bends, degree, road density, distance to the main roads);
* **labels** - from BD TOPO and OSM, with the matching of :mod:`coa_net.scancheck`: a road is real when most of it
  lies within 25 m of a BD TOPO or OSM road; a white road is importance 4 or 5 after the BD TOPO road it lies on;
  a bridge is real when a BD TOPO bridge section is within 50 m; a crossing is grade-separated when a BD TOPO
  section above or below ground is within 50 m of it;
* **models** - one gradient-boosting classifier per decision (LightGBM, CPU, seconds);
* **honest scores** - the sheet is cut into blocks, the blocks into folds; each block is predicted by a model that
  never saw it (spatial cross-validation), and only those out-of-fold predictions are scored;
* **use** - at planning time only the scan goes in: the probabilities replace the fixed rules (unlikely label
  gaps are removed and the others priced by their probability, crossings likely to be a flyover are split,
  white roads likely to be importance 4 become rank 4, unlikely bridges become culverts). On the sheet the
  models were trained on, each block is corrected with its out-of-fold model, so the sanity checks stay honest.

The shapefiles are labels for training and checks for scoring; they are never an input of a plan.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path

import networkx as nx
import numpy as np

log = logging.getLogger(__name__)

L93 = "EPSG:2154"
SYMBOLS = ["blue", "red", "orange", "white", "dashed"]
SYMBOL_CLASS = {"blue": 8, "red": 2, "orange": 3, "white": 6, "dashed": 7}  # class raster value of each symbol (scanroads C_*)
TASKS = ("road_real", "white_imp4", "bridge_real", "crossing_separated", "link_real")
TASK_TEXT = {
    "road_real": "road is real (within 25 m of BD TOPO or OSM on most of its length)",
    "white_imp4": "white road is BD TOPO importance 4 (else 5)",
    "bridge_real": "bridge read on the scan is a BD TOPO bridge (else a culvert or a small crossing)",
    "crossing_separated": "two roads drawn crossing are grade-separated (flyover or underpass)",
    "link_real": "a missing link between a dead end and the road it points at is a real road",
}
CORRECTOR_VERSION = 2


# ---------------------------------------------------------------------------
# image classes and geometry helpers
# ---------------------------------------------------------------------------

def class_raster(tif: str):
    """Palette classes of the whole scan (uint8) and its transform."""
    import rasterio

    from .scanroads import palette_lut

    with rasterio.open(tif) as src:
        lut = palette_lut(src)
        return lut[src.read(1)], src.transform


def _to_px(transform, lonlat: np.ndarray) -> np.ndarray:
    """(row, col) float pixel coordinates of lon/lat points (inverse of scanroads._pix_to_lonlat)."""
    inv = ~transform
    cols, rows = inv * (lonlat[:, 0], lonlat[:, 1])
    return np.column_stack([np.asarray(rows) - 0.5, np.asarray(cols) - 0.5])


def _sample(classes: np.ndarray, rc: np.ndarray) -> np.ndarray:
    H, W = classes.shape
    r = np.rint(rc[..., 0]).astype(int)
    c = np.rint(rc[..., 1]).astype(int)
    inside = (r >= 0) & (r < H) & (c >= 0) & (c < W)
    return np.where(inside, classes[np.clip(r, 0, H - 1), np.clip(c, 0, W - 1)], 0)


def _densify_px(rc: np.ndarray, step: float = 1.0) -> np.ndarray:
    seg = np.diff(rc, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    out = [rc[:1]]
    for p0, s, l in zip(rc[:-1], seg, L):
        n = max(int(l // step), 1)
        t = np.arange(1, n + 1)[:, None] / n
        out.append(p0 + t * s)
    return np.vstack(out)


def _key(G, u, v) -> str:
    a = (round(G.nodes[u]["x"], 6), round(G.nodes[u]["y"], 6))
    b = (round(G.nodes[v]["x"], 6), round(G.nodes[v]["y"], 6))
    a, b = sorted([a, b])
    return f"{a[0]:.6f},{a[1]:.6f},{b[0]:.6f},{b[1]:.6f}"


def _node_key(G, n) -> str:
    return f"{G.nodes[n]['x']:.6f},{G.nodes[n]['y']:.6f}"


def road_table(G: nx.DiGraph):
    """One row per undirected road of the image network (lon/lat and L93 geometry, attributes)."""
    import geopandas as gpd
    import shapely

    seen, rows, geoms = set(), [], []
    for u, v, d in G.edges(data=True):
        k = _key(G, u, v)
        if k in seen:
            continue
        seen.add(k)
        c = d.get("coords") or ((G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"]))
        geoms.append(shapely.LineString(c))
        rows.append({"key": k, "u": u, "v": v, "symbol": d.get("scan_symbol") or "white", "gap": bool(d.get("scan_gap")),
                     "bridge": bool(d.get("bridge")), "stream": bool(d.get("scan_stream")),
                     "width_px": float(d.get("scan_width_px") or np.nan), "casing": float(d.get("scan_casing") or 0.0)})
    gdf = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    gdf["geom93"] = gdf.geometry.to_crs(L93).values
    return gdf


# ---------------------------------------------------------------------------
# features (image and image network only)
# ---------------------------------------------------------------------------

def graph_features(G: nx.DiGraph, roads, n_sources: int = 300, n_targets: int = 300, local_min: float = 15.0,
                   stroke_deg: float = 35.0, seed: int = 0):
    """Network features of each road, from the image network only.

    * ``btw_regional`` / ``btw_local``: sampled betweenness, the number of fastest convoy routes (60 km/h cap)
      between random junctions that use the road, over the whole sheet and for trips under ``local_min`` minutes;
      roads that the network funnels traffic onto are the ones BD TOPO ranks higher;
    * ``stroke_*``: the road continued straight through its junctions (deflection under ``stroke_deg``) as one
      line, as a map reader follows a road: its length, the number of roads in it, its share of white symbol and
      the number of coloured (blue, red, orange) roads it meets.
    """
    import pandas as pd
    import pyproj
    import scipy.sparse as sp
    from scipy.sparse.csgraph import dijkstra

    from .graph import speed_kmh

    nodes = list(G)
    ix = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    r, c, w = [], [], []
    for u, v, d in G.edges(data=True):
        r.append(ix[u])
        c.append(ix[v])
        w.append(d["length_m"] / 1000.0 / min(speed_kmh(d), 60.0) * 60.0)
    A = sp.csr_matrix((w, (r, c)), shape=(n, n))
    rng = np.random.default_rng(seed)
    src = rng.choice(n, min(n_sources, n), replace=False)
    D, P = dijkstra(A, directed=True, indices=src, return_predecessors=True)
    use_r: dict = {}
    use_l: dict = {}
    for k, s0 in enumerate(src):
        pred, dist = P[k], D[k]
        for tt in rng.choice(n, min(n_targets, n), replace=False):
            if not np.isfinite(dist[tt]):
                continue
            local = dist[tt] <= local_min
            x = tt
            while x != s0 and pred[x] >= 0:
                key = (min(pred[x], x), max(pred[x], x))
                use_r[key] = use_r.get(key, 0) + 1
                if local:
                    use_l[key] = use_l.get(key, 0) + 1
                x = pred[x]
    # local trips are few per source: add trips from every source to its own neighbourhood
    for k, s0 in enumerate(src):
        pred, dist = P[k], D[k]
        near = np.flatnonzero(dist <= local_min)
        for tt in rng.choice(near, min(len(near), n_targets), replace=False) if len(near) else []:
            x = tt
            while x != s0 and pred[x] >= 0:
                key = (min(pred[x], x), max(pred[x], x))
                use_l[key] = use_l.get(key, 0) + 1
                x = pred[x]
    ru = np.array([ix[u] for u in roads["u"]])
    rv = np.array([ix[v] for v in roads["v"]])
    keys = [(min(a, b), max(a, b)) for a, b in zip(ru, rv)]
    btw_r = np.log1p([use_r.get(k, 0) for k in keys])
    btw_l = np.log1p([use_l.get(k, 0) for k in keys])

    # strokes: pair the roads at each junction that continue each other best
    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    sym = roads["symbol"].values
    m = len(roads)
    import shapely

    length = shapely.length(np.asarray(roads["geom93"].values, dtype=object))
    ends: dict = {}  # node -> [(road index, heading out of the node)]
    for i, g in enumerate(roads["geom93"].values):
        cc = np.asarray(g.coords)
        if len(cc) < 2:
            continue
        for node, a, b in ((roads["u"].values[i], cc[0], cc[min(3, len(cc) - 1)]), (roads["v"].values[i], cc[-1], cc[max(-4, -len(cc))])):
            h = math.atan2(b[1] - a[1], b[0] - a[0])
            ends.setdefault(node, []).append((i, h))
    parent = list(range(m))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for node, inc in ends.items():
        if len(inc) < 2:
            continue
        pairs = []
        for x in range(len(inc)):
            for y in range(x + 1, len(inc)):
                (i, hi), (j, hj) = inc[x], inc[y]
                if i == j:
                    continue
                dev = abs((math.degrees(hi - hj) + 180.0) % 360.0 - 180.0)  # 180 = straight through
                defl = 180.0 - dev
                if defl <= stroke_deg:
                    pairs.append((defl, i, j))
        used = set()
        for defl, i, j in sorted(pairs):
            if i in used or j in used:
                continue
            used.update((i, j))
            parent[find(i)] = find(j)
    root = np.array([find(i) for i in range(m)])
    s_len = pd.Series(length).groupby(root).transform("sum").values
    s_n = pd.Series(np.ones(m)).groupby(root).transform("sum").values
    s_white = pd.Series(length * (sym == "white")).groupby(root).transform("sum").values / np.maximum(s_len, 1.0)
    coloured_at = {node: sum(1 for i, _ in inc if sym[i] in ("blue", "red", "orange")) for node, inc in ends.items()}
    touch = np.array([coloured_at.get(u, 0) + coloured_at.get(v, 0) for u, v in zip(roads["u"], roads["v"])], float)
    s_col = pd.Series(touch).groupby(root).transform("sum").values
    return pd.DataFrame({"btw_regional": btw_r, "btw_local": btw_l, "stroke_len_m": s_len, "stroke_roads": s_n,
                         "stroke_white_share": s_white, "stroke_coloured_meets": s_col})


def road_features(G: nx.DiGraph, roads, classes: np.ndarray, transform, px_m: float):
    """Features of each road, from the scan and the image network only."""
    import pandas as pd
    import shapely
    from scipy.spatial import cKDTree

    from .scanroads import C_BLACK, C_CASING, C_WATER, C_WHITE

    und = G.to_undirected(as_view=True)
    deg = {n: len(set(G.successors(n)) | set(G.predecessors(n))) for n in G}
    g93 = roads["geom93"].values
    length = shapely.length(g93)
    chord = np.array([math.dist(g.coords[0], g.coords[-1]) for g in g93])
    mid = shapely.get_coordinates(shapely.line_interpolate_point(g93, 0.5, normalized=True))
    nodes93 = None
    import pyproj

    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    nid = list(G.nodes)
    nx_, ny_ = tr.transform([G.nodes[n]["x"] for n in nid], [G.nodes[n]["y"] for n in nid])
    nodes93 = np.column_stack([nx_, ny_])
    node_tree = cKDTree(nodes93)
    density = np.array([len(x) for x in node_tree.query_ball_point(mid, 500.0)])
    feats = {"length_m": length, "sinuosity": length / np.maximum(chord, 1.0), "density_500m": density}
    # distance to the nearest road of each main symbol
    pts, idx = _pts(g93, 50.0)
    sym = roads["symbol"].values
    for s in ("blue", "red", "orange"):
        m = sym[idx] == s
        if m.any():
            d, _ = cKDTree(pts[m]).query(mid)
            feats[f"dist_{s}_m"] = np.minimum(d, 20000.0)
        else:
            feats[f"dist_{s}_m"] = np.full(len(roads), 20000.0)
    # degree of the ends, dead ends, best symbol at each end
    rank = {"blue": 1, "red": 2, "orange": 3, "white": 5, "dashed": 6}
    best_end, deg_u, deg_v = [], [], []
    inc: dict = {}
    for u, v, d in G.edges(data=True):
        r = rank.get(d.get("scan_symbol"), 5)
        inc[u] = min(inc.get(u, 9), r)
        inc[v] = min(inc.get(v, 9), r)
    for u, v in zip(roads["u"], roads["v"]):
        a, b = sorted([deg[u], deg[v]])
        deg_u.append(a)
        deg_v.append(b)
        best_end.append(min(inc.get(u, 9), inc.get(v, 9)))
    feats.update(deg_min=np.array(deg_u), deg_max=np.array(deg_v), best_symbol_at_ends=np.array(best_end))
    # what the scan shows around the road: lettering, water, white, casing within a band
    lettering, water, white, casing_band = [], [], [], []
    offs = np.arange(-6, 7, dtype=float)
    for g in roads.geometry.values:
        rc = _to_px(transform, np.asarray(g.coords))
        rc = _densify_px(rc, 1.0)
        if len(rc) < 2:
            rc = np.vstack([rc, rc])
        t = np.gradient(rc, axis=0)
        t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
        nrm = np.column_stack([-t[:, 1], t[:, 0]])
        q = rc[:, None, :] + offs[None, :, None] * nrm[:, None, :]
        cl = _sample(classes, q)
        near = cl[:, 3:10]
        lettering.append(float((cl == C_BLACK).mean()))
        water.append(float((cl == C_WATER).mean()))
        white.append(float((near == C_WHITE).mean()))
        casing_band.append(float((near == C_CASING).mean()))
    feats.update(lettering=np.array(lettering), water=np.array(water), white_near=np.array(white), casing_near=np.array(casing_band))
    df = pd.DataFrame(feats)
    for s in SYMBOLS:
        df[f"sym_{s}"] = (sym == s).astype(int)
    df["gap"] = roads["gap"].astype(int).values
    df["bridge"] = roads["bridge"].astype(int).values
    df["stream"] = roads["stream"].astype(int).values
    df["width_px"] = roads["width_px"].values
    df["width_m"] = roads["width_px"].values * px_m
    df["casing"] = roads["casing"].values
    gf = graph_features(G, roads)
    for k in gf.columns:
        df[k] = gf[k].values
    return df


def _pts(geoms, step):
    import shapely

    g = shapely.segmentize(np.asarray(geoms), step)
    xy, idx = shapely.get_coordinates(g, return_index=True)
    return xy, idx


def crossing_candidates(G: nx.DiGraph, straight_deg: float = 150.0):
    """Junctions where two pairs of roads (not expressways, not label gaps) each run straight through: an X.
    Returns a list of dicts: node, the two pairs of (neighbour, unit vector, symbol)."""
    cos_s = math.cos(math.radians(straight_deg))
    out = []
    for n in G:
        nbrs = set(G.successors(n)) | set(G.predecessors(n))
        if len(nbrs) < 4:
            continue
        arms = []
        for m in nbrs:
            d = G.edges[n, m] if G.has_edge(n, m) else G.edges[m, n]
            if d.get("scan_gap") or d.get("scan_symbol") == "blue":
                continue
            c = d.get("coords") or ((G.nodes[n]["x"], G.nodes[n]["y"]), (G.nodes[m]["x"], G.nodes[m]["y"]))
            c = np.asarray(c)
            here = np.array([G.nodes[n]["x"], G.nodes[n]["y"]])
            if np.allclose(c[-1], here, atol=1e-7):
                c = c[::-1]
            k = min(len(c) - 1, 3)
            v = c[k] - c[0]
            v = np.array([v[0] * math.cos(math.radians(c[0][1])), v[1]])
            nv = np.hypot(*v)
            if nv == 0:
                continue
            arms.append((m, v / nv, d.get("scan_symbol") or "white"))
        pairs = sorted(((float(np.dot(arms[i][1], arms[j][1])), i, j) for i in range(len(arms)) for j in range(i + 1, len(arms))))
        used, got = set(), []
        for c, i, j in pairs:
            if c > cos_s or i in used or j in used:
                continue
            used |= {i, j}
            got.append((arms[i], arms[j]))
        if len(got) >= 2:
            out.append({"node": n, "pairs": got[:2]})
    return out


def crossing_features(G, cands, classes, transform):
    import pandas as pd

    from .scanroads import C_CASING, C_WATER

    rank = {"red": 2, "orange": 3, "white": 5, "dashed": 6}
    rows = []
    for c in cands:
        n = c["node"]
        p0 = _to_px(transform, np.array([[G.nodes[n]["x"], G.nodes[n]["y"]]]))[0]
        per = []
        for (m1, v1, s1), (m2, v2, s2) in c["pairs"]:
            sym = s1 if rank.get(s1, 5) <= rank.get(s2, 5) else s2
            # direction in pixels: lon/lat vector (east, north) -> (row, col) = (-north, east)
            dpx = np.array([-v1[1], v1[0]])
            ts = np.arange(-7, 8, dtype=float)
            line = p0[None, :] + ts[:, None] * dpx[None, :]
            cl = _sample(classes, line)
            own = SYMBOL_CLASS.get(sym, 6)
            per.append({"rank": rank.get(sym, 5), "own_share": float((cl == own).mean()), "casing_share": float((cl == C_CASING).mean()),
                        "centre_own": float((cl[5:10] == own).mean()), "dir": dpx})
        per.sort(key=lambda r: r["rank"])
        a, b = per
        ang = math.degrees(math.acos(min(1.0, abs(float(np.dot(a["dir"], b["dir"]))))))
        disc = _sample(classes, p0[None, :] + np.array([[i, j] for i in range(-6, 7) for j in range(-6, 7)], float))
        rows.append({"rank_a": a["rank"], "rank_b": b["rank"], "own_a": a["own_share"], "own_b": b["own_share"],
                     "centre_own_a": a["centre_own"], "centre_own_b": b["centre_own"], "casing_a": a["casing_share"],
                     "casing_b": b["casing_share"], "angle_deg": 90 - ang, "water_disc": float((disc == C_WATER).mean()),
                     "degree": len(set(G.successors(n)) | set(G.predecessors(n)))})
    return pd.DataFrame(rows)


def _end_heading(G, n, m, tr):
    """Unit vector (metres, L93) pointing out of dead end ``n`` along its road to ``m``, from about 40 m in."""
    d = G.edges[n, m] if G.has_edge(n, m) else G.edges[m, n]
    c = np.asarray(d.get("coords") or ((G.nodes[n]["x"], G.nodes[n]["y"]), (G.nodes[m]["x"], G.nodes[m]["y"])), float)
    here = np.array([G.nodes[n]["x"], G.nodes[n]["y"]])
    if not np.allclose(c[0], here, atol=1e-7):
        c = c[::-1]
    x, y = tr.transform(c[:, 0], c[:, 1])
    xy = np.column_stack([x, y])
    seg = np.hypot(*np.diff(xy, axis=0).T)
    k = int(np.searchsorted(np.cumsum(seg), 40.0)) + 1
    v = xy[0] - xy[min(k, len(xy) - 1)]
    nv = float(np.hypot(*v))
    return (v / nv if nv > 0 else None), d


LINK_RANK = {"blue": 1, "red": 2, "orange": 3, "white": 5, "dashed": 6}


def link_candidates(G: nx.DiGraph, max_m: float = 400.0, max_to_line_m: float = 200.0, cone_deg: float = 45.0,
                    per_end: int = 2) -> list[dict]:
    """Links the image network may be missing: a dead end facing another dead end (within ``max_m``, both
    pointing at each other) or facing a junction of another road (within ``max_to_line_m``), when the network
    has no short path between them. A road drawn under an expressway, a railway or a label is cut on the map;
    the reader's gap rule joins only to the nearest line ahead, which is often the band itself."""
    import pyproj
    from scipy.spatial import cKDTree

    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    nid = list(G.nodes)
    xs, ys = tr.transform([G.nodes[n]["x"] for n in nid], [G.nodes[n]["y"] for n in nid])
    xy = np.column_stack([xs, ys])
    pos = {n: i for i, n in enumerate(nid)}
    tree = cKDTree(xy)
    und = G.to_undirected(as_view=True)
    nbrs = {n: set(G.successors(n)) | set(G.predecessors(n)) for n in G}
    dead = {}
    for n in G:
        if len(nbrs[n]) == 1:
            m = next(iter(nbrs[n]))
            h, d = _end_heading(G, n, m, tr)
            if h is not None and d.get("scan_symbol") not in ("blue", "dashed") and d.get("road_rank", 5) <= 5:
                dead[n] = (h, d)
    cos_c = math.cos(math.radians(cone_deg))
    out, seen = [], set()
    for a, (ha, da) in dead.items():
        pa = xy[pos[a]]
        cands = []
        for j in tree.query_ball_point(pa, max_m):
            b = nid[j]
            if b == a or b in nbrs[a]:
                continue
            v = xy[j] - pa
            dist = float(np.hypot(*v))
            if dist < 15.0:
                continue
            u = v / dist
            ca = float(np.dot(ha, u))
            if ca < cos_c:
                continue
            if b in dead:
                cb = float(np.dot(dead[b][0], -u))
                if cb < cos_c:
                    continue
                kind = 1
            else:
                if dist > max_to_line_m:
                    continue
                if all((G.edges[b, m] if G.has_edge(b, m) else G.edges[m, b]).get("scan_symbol") == "blue" for m in nbrs[b]):
                    continue  # never join onto an expressway (it has no level junction)
                cb, kind = -1.0, 0
            cands.append((dist * (2.0 - ca - max(cb, 0.0)), b, dist, ca, cb, kind))
        cands.sort()
        for _, b, dist, ca, cb, kind in cands[:per_end]:
            key = tuple(sorted((pos[a], pos[b])))
            if key in seen:
                continue
            try:
                gd = nx.shortest_path_length(und, a, b, weight="length_m") if nx.has_path(und, a, b) else float("inf")
            except nx.NetworkXNoPath:
                gd = float("inf")
            if gd < 3.0 * dist:
                continue
            seen.add(key)
            out.append({"a": a, "b": b, "dist_m": dist, "cos_a": ca, "cos_b": cb, "b_dead_end": kind, "graph_ratio": min(gd / dist, 100.0),
                        "xy_a": tuple(pa), "xy_b": tuple(xy[pos[b]]),
                        "key_a": _node_key(G, a), "key_b": _node_key(G, b)})
    return out


def link_features(G, links, classes, transform, roads=None, X_roads=None):
    """Features of each candidate link, from the scan and the image network only."""
    import pandas as pd

    from .scanroads import C_BLACK, C_BLUE, C_CASING, C_CASING_LIGHT, C_OTHER, C_RED, C_ORANGE, C_WATER, C_WHITE

    btw = {}
    if roads is not None and X_roads is not None and "btw_regional" in X_roads:
        for u, v, b in zip(roads["u"].values, roads["v"].values, X_roads["btw_regional"].values):
            btw[u] = max(btw.get(u, 0.0), b)
            btw[v] = max(btw.get(v, 0.0), b)
    nbrs = lambda n: set(G.successors(n)) | set(G.predecessors(n))

    def road_of(n):
        best = None
        for m in nbrs(n):
            d = G.edges[n, m] if G.has_edge(n, m) else G.edges[m, n]
            if best is None or LINK_RANK.get(d.get("scan_symbol"), 5) < LINK_RANK.get(best.get("scan_symbol"), 5):
                best = d
        return best or {}

    rows = []
    for c in links:
        a, b = c["a"], c["b"]
        da, db = road_of(a), road_of(b)
        p = _to_px(transform, np.array([[G.nodes[a]["x"], G.nodes[a]["y"]], [G.nodes[b]["x"], G.nodes[b]["y"]]]))
        line = _densify_px(p, 1.0)
        cl = _sample(classes, line)
        blue = cl == C_BLUE
        run, best = 0, 0
        for x in blue:
            run = run + 1 if x else 0
            best = max(best, run)
        px_m = c["dist_m"] / max(len(line) - 1, 1)
        ra, rb = LINK_RANK.get(da.get("scan_symbol"), 5), LINK_RANK.get(db.get("scan_symbol"), 5)
        rows.append({"dist_m": c["dist_m"], "cos_a": c["cos_a"], "cos_b": c["cos_b"], "b_dead_end": c["b_dead_end"],
                     "graph_ratio": c["graph_ratio"], "rank_a": ra, "rank_b": rb, "same_symbol": int(da.get("scan_symbol") == db.get("scan_symbol")),
                     "len_a_m": float(da.get("length_m") or 0.0), "len_b_m": float(db.get("length_m") or 0.0),
                     "gap_a": int(bool(da.get("scan_gap"))), "deg_b": len(nbrs(b)),
                     "sh_blue": float(blue.mean()), "blue_run_m": best * px_m, "sh_casing": float((cl == C_CASING).mean()),
                     "sh_casing_light": float((cl == C_CASING_LIGHT).mean()), "sh_water": float((cl == C_WATER).mean()),
                     "sh_black": float((cl == C_BLACK).mean()), "sh_white": float((cl == C_WHITE).mean()),
                     "sh_fill": float(np.isin(cl, (C_RED, C_ORANGE)).mean()), "sh_other": float((cl == C_OTHER).mean()),
                     "btw_a": btw.get(a, 0.0), "btw_b": btw.get(b, 0.0)})
    return pd.DataFrame(rows)


def link_labels(links, bdtopo: str, osm: str | None, step_m: float = 10.0, buffer_m: float = 25.0, need: float = 0.8):
    """A candidate link is real when at least ``need`` of its straight line lies within ``buffer_m`` of BD TOPO or OSM."""
    from scipy.spatial import cKDTree

    from .scancheck import reference_lines

    if not links:
        return np.zeros(0)
    bd, om = reference_lines(bdtopo, osm)
    ref = [_pts(bd.geometry.values, step_m)[0]]
    if om is not None and len(om):
        ref.append(_pts(om.geometry.values, step_m)[0])
    T = cKDTree(np.vstack(ref))
    y = np.zeros(len(links))
    for i, c in enumerate(links):
        a, b = np.asarray(c["xy_a"]), np.asarray(c["xy_b"])
        k = max(2, int(c["dist_m"] / step_m) + 1)
        q = a[None, :] + np.linspace(0, 1, k)[:, None] * (b - a)[None, :]
        d, _ = T.query(q, distance_upper_bound=buffer_m + 1)
        y[i] = float((d <= buffer_m).mean() >= need)
    return y


# ---------------------------------------------------------------------------
# labels (shapefiles; training and scoring only)
# ---------------------------------------------------------------------------

def road_labels(roads, bdtopo: str, osm: str | None, step_m: float = 10.0, buffer_m: float = 25.0):
    """Labels for each road: real (share within the buffer of BD TOPO or OSM), BD TOPO importance it lies on,
    BD TOPO bridge within 50 m (for bridges)."""
    import pandas as pd
    from scipy.spatial import cKDTree

    from .scancheck import reference_lines

    bd, om = reference_lines(bdtopo, osm)
    bxy, bidx = _pts(bd.geometry.values, step_m)
    bcls = bd["cls"].values[bidx]
    ref = [bxy]
    if om is not None and len(om):
        oxy, _ = _pts(om.geometry.values, step_m)
        ref.append(oxy)
    T_any = cKDTree(np.vstack(ref))
    T_bd = cKDTree(bxy)
    sxy, sidx = _pts(roads["geom93"].values, step_m)
    d_any, _ = T_any.query(sxy, distance_upper_bound=buffer_m + 1)
    d_bd, j_bd = T_bd.query(sxy, distance_upper_bound=buffer_m + 1)
    n = len(roads)
    tot = np.bincount(sidx, minlength=n).astype(float)
    real = np.bincount(sidx, weights=(d_any <= buffer_m).astype(float), minlength=n) / np.maximum(tot, 1)
    ok = d_bd <= buffer_m
    is4 = np.bincount(sidx[ok], weights=(bcls[j_bd[ok]] == "4").astype(float), minlength=n)
    is5 = np.bincount(sidx[ok], weights=(bcls[j_bd[ok]] == "5 minor").astype(float), minlength=n)
    br = bd[bd["bridge"]]
    mid = np.asarray([g.interpolate(0.5, normalized=True).coords[0] for g in roads["geom93"].values])
    if len(br):
        bb, _ = _pts(br.geometry.values, 10.0)
        d_br, _ = cKDTree(bb).query(mid)
    else:
        d_br = np.full(n, np.inf)
    lab = pd.DataFrame({"real_share": real, "road_real": (real >= 0.5).astype(int)})
    w = is4 + is5
    lab["white_imp4"] = np.where((roads["symbol"].values == "white") & (w >= 0.5 * tot) & (tot > 0), (is4 > is5).astype(float), np.nan)
    lab["bridge_real"] = np.where(roads["bridge"].values, (d_br <= 50.0).astype(float), np.nan)
    return lab, bd


def crossing_labels(G, cands, bd, radius_m: float = 50.0):
    import pyproj
    from scipy.spatial import cKDTree

    sep = bd[bd["POS_SOL"].fillna(0).astype(int) != 0]
    if not len(cands):
        return np.zeros(0)
    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    x, y = tr.transform([G.nodes[c["node"]]["x"] for c in cands], [G.nodes[c["node"]]["y"] for c in cands])
    if not len(sep):
        return np.zeros(len(cands))
    pts, _ = _pts(sep.geometry.values, 10.0)
    d, _ = cKDTree(pts).query(np.column_stack([x, y]))
    return (d <= radius_m).astype(float)


# ---------------------------------------------------------------------------
# dataset, spatial cross-validation, models
# ---------------------------------------------------------------------------

def blocks_of(lonlat: np.ndarray, bounds: tuple, n: int = 4) -> np.ndarray:
    """Block id (0 .. n*n-1) of each point on an n x n grid over the sheet."""
    x0, y0, x1, y1 = bounds
    i = np.clip(((lonlat[:, 0] - x0) / (x1 - x0) * n).astype(int), 0, n - 1)
    j = np.clip(((lonlat[:, 1] - y0) / (y1 - y0) * n).astype(int), 0, n - 1)
    return j * n + i


def fold_of_block(n_blocks: int, n_folds: int = 4, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    f = np.arange(n_blocks) % n_folds
    rng.shuffle(f)
    return f


def build_dataset(G: nx.DiGraph, tif: str, bdtopo: str, osm: str | None, grid: int = 4, progress=print):
    """Features, labels and block of every road and crossing of the image network."""
    import rasterio

    t = time.perf_counter()
    classes, transform = class_raster(tif)
    with rasterio.open(tif) as src:
        b = src.bounds
        bounds = (b.left, b.bottom, b.right, b.top)
        lat0 = (b.top + b.bottom) / 2
        px_m = (abs(src.transform.a) * 111_320 * math.cos(math.radians(lat0)) + abs(src.transform.e) * 110_570) / 2
    roads = road_table(G)
    X = road_features(G, roads, classes, transform, px_m)
    progress(f"  road features: {len(X)} roads, {time.perf_counter() - t:.0f} s")
    t = time.perf_counter()
    Y, bd = road_labels(roads, bdtopo, osm)
    mid = np.asarray([g.interpolate(0.5, normalized=True).coords[0] for g in roads.geometry.values])
    blk = blocks_of(mid, bounds, grid)
    cands = crossing_candidates(G)
    Xc = crossing_features(G, cands, classes, transform)
    Yc = crossing_labels(G, cands, bd)
    cxy = np.asarray([[G.nodes[c["node"]]["x"], G.nodes[c["node"]]["y"]] for c in cands]).reshape(-1, 2)
    blk_c = blocks_of(cxy, bounds, grid) if len(cands) else np.zeros(0, int)
    progress(f"  labels and crossings: {len(cands)} crossings, {time.perf_counter() - t:.0f} s")
    t = time.perf_counter()
    links = link_candidates(G)
    Xl = link_features(G, links, classes, transform, roads, X)
    Yl = link_labels(links, bdtopo, osm)
    lxy = np.asarray([[(G.nodes[c["a"]]["x"] + G.nodes[c["b"]]["x"]) / 2, (G.nodes[c["a"]]["y"] + G.nodes[c["b"]]["y"]) / 2] for c in links]).reshape(-1, 2)
    blk_l = blocks_of(lxy, bounds, grid) if len(links) else np.zeros(0, int)
    progress(f"  candidate links: {len(links)} ({Yl.mean() if len(Yl) else 0:.0%} real), {time.perf_counter() - t:.0f} s")
    return {"roads": roads, "X": X, "Y": Y, "block": blk, "cands": cands, "Xc": Xc, "Yc": Yc, "block_c": blk_c,
            "links": links, "Xl": Xl, "Yl": Yl, "block_l": blk_l, "bounds": bounds, "grid": grid}


def _task_rows(ds, task):
    """(features, labels, blocks, row index into the element table, rule baseline prediction) of one task."""
    if task == "crossing_separated":
        X, y, b = ds["Xc"], ds["Yc"], ds["block_c"]
        idx = np.arange(len(y))
        return X, y, b, idx, np.zeros(len(y))  # the rule: a crossing drawn on the map is a crossroads
    if task == "link_real":
        X, y, b = ds["Xl"], ds["Yl"], ds["block_l"]
        return X, y, b, np.arange(len(y)), np.zeros(len(y))  # the rule: no link beyond the label-gap joins
    X, Y, b = ds["X"], ds["Y"], ds["block"]
    if task == "road_real":
        m = np.ones(len(X), bool)
        base = np.ones(len(X))  # the rule keeps every road
    elif task == "white_imp4":
        m = ~np.isnan(Y["white_imp4"].values)
        base = np.zeros(len(X))  # the rule: every white road is rank 5
    else:  # bridge_real
        m = ~np.isnan(Y["bridge_real"].values)
        base = np.ones(len(X))  # the rule: light blue water on both sides is a bridge
    idx = np.flatnonzero(m)
    return X.iloc[idx].reset_index(drop=True), Y[task].values[idx].astype(float), b[idx], idx, base[idx]


LGB_PARAMS = dict(n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=20, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, reg_lambda=1.0, verbose=-1)


def _fit(X, y, seed=0):
    import lightgbm as lgb

    m = lgb.LGBMClassifier(**LGB_PARAMS, random_state=seed)
    m.fit(X, y)
    return m


def cross_validate(ds, n_folds: int = 4, seed: int = 0, progress=print) -> dict:
    """Spatial cross-validation: blocks are assigned to folds; each fold is predicted by a model trained on the
    other folds. Returns per task the out-of-fold probabilities and the scores (on unseen blocks only)."""
    from sklearn.metrics import accuracy_score, f1_score, roc_auc_score

    fob = fold_of_block(ds["grid"] ** 2, n_folds, seed)
    res = {"folds_of_blocks": fob.tolist(), "tasks": {}}
    for task in TASKS:
        X, y, b, idx, base = _task_rows(ds, task)
        if len(y) < 50 or len(np.unique(y)) < 2:
            res["tasks"][task] = {"n": int(len(y)), "skipped": "too few examples or one class only"}
            continue
        fold = fob[b]
        oof = np.full(len(y), np.nan)
        for f in range(n_folds):
            tr, te = fold != f, fold == f
            if te.sum() == 0 or len(np.unique(y[tr])) < 2:
                continue
            oof[te] = _fit(X[tr], y[tr], seed).predict_proba(X[te])[:, 1]
        ok = ~np.isnan(oof)
        yp = (oof[ok] >= 0.5).astype(int)
        s = {"n": int(len(y)), "positives": float(y.mean()), "auc": float(roc_auc_score(y[ok], oof[ok])),
             "accuracy_model": float(accuracy_score(y[ok], yp)), "accuracy_rule": float(accuracy_score(y[ok], base[ok])),
             "f1_model": float(f1_score(y[ok], yp)), "f1_rule": float(f1_score(y[ok], base[ok]))}
        if task == "road_real":
            gap = X["gap"].values.astype(bool)
            g = ok & gap
            if g.any():
                s["gaps"] = {"n": int(g.sum()), "real": float(y[g].mean()), "auc": float(roc_auc_score(y[g], oof[g])) if len(np.unique(y[g])) > 1 else None}
        res["tasks"][task] = s
        res["tasks"][task]["_oof"] = oof
        res["tasks"][task]["_idx"] = idx
        progress(f"  {task}: n={s['n']}, positives {s['positives']:.0%}, AUC {s['auc']:.3f}, accuracy {s['accuracy_model']:.1%} "
                 f"(rule {s['accuracy_rule']:.1%})")
    res["decisions"] = decision_scores(ds, res)
    for k, d in res["decisions"].items():
        progress(f"  {k}: {d['changes']} changes on unseen blocks, {d['precision']:.0%} right (base rate {d['base']:.0%}) -> "
                 f"{'on' if d['enabled'] else 'off'}")
    return res


def decision_scores(ds, cv, thresh: dict | None = None) -> dict:
    """For each correction, on the out-of-fold predictions: how many changes it makes, the share that are right,
    the base rate of the same label among the candidates, and whether it passes the gate."""
    th = dict(THRESH, **(thresh or {}))
    out = {}
    for name, (task, rows, when, key, right) in DECISIONS.items():
        t = cv["tasks"].get(task, {})
        if "_oof" not in t:
            out[name] = {"changes": 0, "precision": 0.0, "base": 0.0, "lift": 0.0, "enabled": False, "threshold": th[key]}
            continue
        X, y, *_ = _task_rows(ds, task)
        p = t["_oof"]
        ok = ~np.isnan(p)
        if rows == "gap":
            ok &= X["gap"].values == 1
        elif rows == "road":
            ok &= X["gap"].values == 0
        ch = ok & ((p < th[key]) if when == "below" else (p >= th[key]))
        is_right = (y == right)
        prec = float(is_right[ch].mean()) if ch.any() else 0.0
        base = float(is_right[ok].mean()) if ok.any() else 0.0
        lift = prec / base if base else 0.0
        d = {"changes": int(ch.sum()), "precision": prec, "base": base, "lift": lift, "threshold": th[key],
             "enabled": bool(ch.sum() >= GATE_MIN and (prec >= GATE_ADD_PRECISION if name in ADDITIONS else
                                                         prec >= GATE_PRECISION and lift >= GATE_LIFT))}
        if "length_m" in X:
            d["km"] = float(X["length_m"].values[ch].sum() / 1000)
        elif "dist_m" in X:
            d["km"] = float(X["dist_m"].values[ch].sum() / 1000)
        out[name] = d
    return out


def train_final(ds, seed: int = 0) -> dict:
    """One model per task on every block (to correct another sheet)."""
    models = {}
    for task in TASKS:
        X, y, *_ = _task_rows(ds, task)
        if len(y) >= 50 and len(np.unique(y)) == 2:
            models[task] = _fit(X, y, seed)
    return models


def feature_importance(models: dict) -> dict:
    out = {}
    for t, m in models.items():
        imp = m.booster_.feature_importance("gain")
        names = m.booster_.feature_name()
        tot = imp.sum() or 1.0
        out[t] = {n: round(float(v / tot), 3) for n, v in sorted(zip(names, imp), key=lambda kv: -kv[1])[:8]}
    return out


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

THRESH = {"drop_gap": 0.5, "drop_road": 0.3, "uncertain_road": 0.7, "imp4": 0.7, "bridge": 0.3, "separate": 0.5, "link": 0.5}
# removing a road the model doubts can cut a village off (about 18 % of the removals hit real roads): by default
# doubtful roads and label gaps are priced, never removed; ``scan-learn --allow-removal`` switches removal back on
REMOVALS = {"drop_gap", "drop_road"}
GAP_CAUTION = 3.0   # a label-gap join costs 1 + GAP_CAUTION x (1 - p) times its time
ROAD_CAUTION = 3.0  # another road costs 1 + ROAD_CAUTION x (uncertain_road - p) times when p is below uncertain_road
# a correction is switched on only if, on blocks the model never saw, most of the changes it would make are right
# and it does clearly better than the fixed rule's own error rate
GATE_PRECISION, GATE_LIFT, GATE_MIN = 0.6, 1.5, 10
# an addition (a link the reader did not draw) is not a change of a fixed rule's decision, so there is no rule error
# rate to beat: it is switched on when most of the links it adds are real, with a stricter bar
GATE_ADD_PRECISION = 0.7
ADDITIONS = {"add_link"}
DECISIONS = {  # name: (task, rows, change when, threshold key, a change is right when the label is)
    "drop_gap": ("road_real", "gap", "below", "drop_gap", 0),
    "drop_road": ("road_real", "road", "below", "drop_road", 0),
    "white_to_rank4": ("white_imp4", "all", "above", "imp4", 1),
    "bridge_to_culvert": ("bridge_real", "all", "below", "bridge", 0),
    "separate_crossing": ("crossing_separated", "all", "above", "separate", 1),
    "add_link": ("link_real", "all", "above", "link", 1),
}


def predictions_from_oof(ds, cv) -> dict:
    """Out-of-fold probabilities keyed by road (key) and crossing (node key) for this sheet."""
    roads = ds["roads"]
    P = {"roads": {}, "crossings": {}}
    for task in ("road_real", "white_imp4", "bridge_real"):
        t = cv["tasks"].get(task, {})
        if "_oof" not in t:
            continue
        for i, p in zip(t["_idx"], t["_oof"]):
            if not np.isnan(p):
                P["roads"].setdefault(roads["key"].values[i], {})[task] = float(p)
    t = cv["tasks"].get("crossing_separated", {})
    if "_oof" in t:
        P["crossing_nodes"] = [c["node"] for c in ds["cands"]]
        P["crossings"] = {i: float(p) for i, p in zip(t["_idx"], t["_oof"]) if not np.isnan(p)}
    t = cv["tasks"].get("link_real", {})
    if "_oof" in t:
        P["links"] = [[c["key_a"], c["key_b"], float(p)] for c, p in zip(ds["links"], t["_oof"]) if not np.isnan(p)]
    return P


def predictions_from_models(G, tif, models, cnn_dir=None) -> dict:
    """Probabilities for a sheet the models were not trained on (no shapefile needed)."""
    import rasterio

    classes, transform = class_raster(tif)
    with rasterio.open(tif) as src:
        b = src.bounds
        lat0 = (b.top + b.bottom) / 2
        px_m = (abs(src.transform.a) * 111_320 * math.cos(math.radians(lat0)) + abs(src.transform.e) * 110_570) / 2
    roads = road_table(G)
    X = road_features(G, roads, classes, transform, px_m)
    P = {"roads": {}, "crossings": {}}
    for task in ("road_real", "white_imp4", "bridge_real"):
        if task not in models:
            continue
        if task == "white_imp4":
            m = roads["symbol"].values == "white"
        elif task == "bridge_real":
            m = roads["bridge"].values
        else:
            m = np.ones(len(roads), bool)
        idx = np.flatnonzero(m)
        if not len(idx):
            continue
        p = models[task].predict_proba(X.iloc[idx])[:, 1]
        for i, pi in zip(idx, p):
            P["roads"].setdefault(roads["key"].values[i], {})[task] = float(pi)
    cands = crossing_candidates(G)
    if cands and "crossing_separated" in models:
        Xc = crossing_features(G, cands, classes, transform)
        P["crossing_nodes"] = [c["node"] for c in cands]
        P["crossings"] = dict(enumerate(models["crossing_separated"].predict_proba(Xc)[:, 1].astype(float)))
    if cnn_dir is not None:  # crop networks replace the tabular models where they were better on unseen blocks
        from . import scancnn

        cd = Path(cnn_dir)
        if (cd / "cnn_bridge_real.pt").exists():
            idx = np.flatnonzero(roads["bridge"].values)
            if len(idx):
                mid = np.asarray([g.interpolate(0.5, normalized=True).coords[0] for g in roads.geometry.values[idx]])
                p = scancnn.predict_saved(cd / "cnn_bridge_real.pt", scancnn.crops(classes, _to_px(transform, mid)))
                for i, pi in zip(idx, p):
                    P["roads"].setdefault(roads["key"].values[i], {})["bridge_real"] = float(pi)
        if (cd / "cnn_crossing_separated.pt").exists() and cands:
            xy = np.asarray([[G.nodes[c["node"]]["x"], G.nodes[c["node"]]["y"]] for c in cands])
            p = scancnn.predict_saved(cd / "cnn_crossing_separated.pt", scancnn.crops(classes, _to_px(transform, xy)))
            P["crossing_nodes"] = [c["node"] for c in cands]
            P["crossings"] = dict(enumerate(p.astype(float)))
    if "link_real" in models:
        links = link_candidates(G)
        if links:
            Xl = link_features(G, links, classes, transform, roads, X)
            p = models["link_real"].predict_proba(Xl)[:, 1]
            P["links"] = [[_node_key(G, c["a"]), _node_key(G, c["b"]), float(pi)] for c, pi in zip(links, p)]
    return P


def apply_corrections(G: nx.DiGraph, P: dict, thresh: dict | None = None, enabled: set | None = None) -> dict:
    """Apply the probabilities to the image network, in place. ``enabled``: the corrections that passed the gate
    on unseen blocks (all when None). The probability that a road exists is always recorded (``scan_p_real``)
    and prices the road (:func:`uncertainty_penalty`). Returns what was changed."""
    from .graph import road_rank
    from .realdata import keep_largest_scc

    th = dict(THRESH, **(thresh or {}))
    on = set(DECISIONS) if enabled is None else set(enabled)
    ch = {"label gaps removed": 0.0, "roads removed": 0.0, "roads priced as doubtful (p < 0.7)": 0.0, "label gaps priced": 0.0, "white roads to rank 4": 0.0,
          "bridges to culverts": 0.0, "crossings separated": 0, "links added": 0}
    # crossings first (they need the junctions as they are)
    if "separate_crossing" in on:
        nodes = P.get("crossing_nodes") or []
        cands = {c["node"]: c for c in crossing_candidates(G)}
        next_id = max((n for n in G if isinstance(n, int)), default=0) + 1
        rank = {"red": 2, "orange": 3, "white": 5, "dashed": 6}
        for i, p in (P.get("crossings") or {}).items():
            n = nodes[int(i)]
            if p < th["separate"] or n not in cands or n not in G:
                continue
            # the lower-class pair goes over or under: it gets its own node
            pairs = sorted(cands[n]["pairs"], key=lambda pr: min(rank.get(pr[0][2], 5), rank.get(pr[1][2], 5)))
            (m1, _, _), (m2, _, _) = pairs[-1]
            new, next_id = next_id, next_id + 1
            G.add_node(new, **dict(G.nodes[n]))
            G.nodes[new]["x"] = G.nodes[n]["x"] + 1e-7
            for m in (m1, m2):
                for a, b in ((n, m), (m, n)):
                    if G.has_edge(a, b):
                        d = dict(G.edges[a, b])
                        G.remove_edge(a, b)
                        G.add_edge(new if a == n else a, new if b == n else b, **d)
            ch["crossings separated"] += 1
    by_key: dict = {}
    for u, v in list(G.edges):
        by_key.setdefault(_key(G, u, v), []).append((u, v))
    for k, probs in (P.get("roads") or {}).items():
        for u, v in by_key.get(k, []):
            if not G.has_edge(u, v):
                continue
            d = G.edges[u, v]
            pr = probs.get("road_real")
            if pr is not None:
                d["scan_p_real"] = round(pr, 3)
                if d.get("scan_gap") and "drop_gap" in on and pr < th["drop_gap"]:
                    G.remove_edge(u, v)
                    ch["label gaps removed"] += 0.5
                    continue
                if not d.get("scan_gap") and "drop_road" in on and pr < th["drop_road"]:
                    G.remove_edge(u, v)
                    ch["roads removed"] += 0.5
                    continue
                if not d.get("scan_gap") and pr < th["uncertain_road"]:
                    ch["roads priced as doubtful (p < 0.7)"] += 0.5
                if d.get("scan_gap"):
                    ch["label gaps priced"] += 0.5
            p4 = probs.get("white_imp4")
            if p4 is not None and d.get("scan_symbol") == "white":
                d["scan_p_imp4"] = round(p4, 3)
                if "white_to_rank4" in on and p4 >= th["imp4"]:
                    d["highway"] = "tertiary"
                    d["road_rank"] = road_rank("tertiary")
                    ch["white roads to rank 4"] += 0.5
            pb = probs.get("bridge_real")
            if pb is not None and d.get("bridge"):
                d["scan_p_bridge"] = round(pb, 3)
                if "bridge_to_culvert" in on and pb < th["bridge"]:
                    d["bridge"] = False
                    d["scan_stream"] = True
                    ch["bridges to culverts"] += 0.5
    if "add_link" in on and P.get("links"):
        import pyproj

        tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
        by_node = {_node_key(G, n): n for n in G}
        for ka, kb, p in P["links"]:
            a, b = by_node.get(ka), by_node.get(kb)
            if a is None or b is None or p < th["link"] or G.has_edge(a, b):
                continue
            nb = lambda n: [G.edges[n, m] if G.has_edge(n, m) else G.edges[m, n] for m in set(G.successors(n)) | set(G.predecessors(n))]
            ends = [d for d in nb(a) + nb(b) if d.get("scan_symbol") != "blue"]
            if not ends:
                continue
            src = min(ends, key=lambda d: LINK_RANK.get(d.get("scan_symbol"), 5)) if len(nb(b)) == 1 else nb(a)[0]
            (xa, ya), (xb, yb) = (G.nodes[a]["x"], G.nodes[a]["y"]), (G.nodes[b]["x"], G.nodes[b]["y"])
            X2, Y2 = tr.transform([xa, xb], [ya, yb])
            L = float(math.hypot(X2[1] - X2[0], Y2[1] - Y2[0]))
            keep = ("highway", "paved", "scan_symbol", "scan_width_px", "road_rank", "source_id", "width_m")
            base = {k: src.get(k) for k in keep}
            base.update(bridge=False, tunnel=False, scan_gap=True, scan_link=True, scan_stream=False, scan_p_real=round(p, 3),
                        length_m=L, name="")
            for k in ("built_up", "forest", "closed", "level_crossing", "ford", "ferry", "defile"):
                base[k] = False
            for k in ("threat", "weather", "centrality", "closed_p", "threat0", "weather0"):
                base[k] = 0.0
            for k in ("maxspeed_kmh", "lanes", "max_weight_t", "mlc", "max_height_m", "slope_pct", "lit"):
                base[k] = None
            G.add_edge(a, b, **dict(base, coords=((xa, ya), (xb, yb))))
            G.add_edge(b, a, **dict(base, coords=((xb, yb), (xa, ya))))
            ch["links added"] = ch.get("links added", 0) + 1
    G.remove_nodes_from([n for n in list(G) if G.degree(n) == 0])
    keep_largest_scc(G)
    out = {k: int(round(v)) for k, v in ch.items()}
    G.graph["scan_corrected"] = out
    return out


def uncertainty_penalty(d: dict) -> float:
    """Cost factor of an image road from the corrector's probability that it exists (1 when sure or unknown).

    Cautious by design: a label-gap join costs 1 + 3 (1 - p) times its time (about 1.3 for the typical join, which
    is real 85 % of the time; 2.5 and more for a join the model doubts), so the calibrated probability makes routing
    more careful than the fixed 1.5, not less; another road costs 1 + 3 (0.7 - p) times below p = 0.7 (up to 3.1).
    Without a corrector a label gap costs 1.5 times its time, as before."""
    p = d.get("scan_p_real")
    if p is None:
        return 1.5 if d.get("scan_gap") else 1.0
    if d.get("scan_gap"):
        return 1.0 + GAP_CAUTION * (1.0 - p)
    return 1.0 + ROAD_CAUTION * max(0.0, THRESH["uncertain_road"] - p)


# ---------------------------------------------------------------------------
# saving, loading, report
# ---------------------------------------------------------------------------

def road_gate_enabled(cv: dict, allow_removal: bool = False) -> set:
    """The corrections that pass the road-level gate (removals only when allowed)."""
    return {k for k, d in cv.get("decisions", {}).items() if d["enabled"] and (allow_removal or k not in REMOVALS)}


def save(out: Path, models: dict, P: dict, cv: dict, ds, tif: str, graph_key: str, allow_removal: bool = False,
         route_gate: dict | None = None, extra_enabled: set | None = None, extra_meta: dict | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for t, m in models.items():
        m.booster_.save_model(str(out / f"model_{t}.txt"))
    enabled = road_gate_enabled(cv, allow_removal)
    thresholds = dict(THRESH)
    if route_gate:
        enabled = set(route_gate["enabled"]) | (enabled & REMOVALS)
        thresholds.update(route_gate["thresholds"])
    enabled |= set(extra_enabled or ())
    meta = {"version": CORRECTOR_VERSION, "trained_on": str(tif), "graph_key": graph_key, "thresholds": thresholds,
            "decisions": cv.get("decisions", {}), "allow_removal": allow_removal, "route_gate": route_gate,
            "enabled": sorted(enabled),
            "features_roads": list(ds["X"].columns), "features_crossings": list(ds["Xc"].columns),
            "features_links": list(ds["Xl"].columns) if "Xl" in ds else [], **(extra_meta or {})}
    (out / "corrector.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    Pj = {"roads": P["roads"], "crossings": {str(k): v for k, v in (P.get("crossings") or {}).items()},
          "crossing_nodes": [str(n) for n in P.get("crossing_nodes") or []], "links": P.get("links") or []}
    (out / "oof_predictions.json").write_text(json.dumps(Pj), encoding="utf-8")


class _Booster:
    """A saved LightGBM model with the ``predict_proba`` of the scikit-learn wrapper."""

    def __init__(self, path):
        import lightgbm as lgb

        self.b = lgb.Booster(model_file=str(path))

    def predict_proba(self, X):
        p = self.b.predict(X)
        return np.column_stack([1 - p, p])


def load_corrector(path: str | Path) -> tuple[dict, dict]:
    path = Path(path)
    meta = json.loads((path / "corrector.json").read_text(encoding="utf-8"))
    models = {t: _Booster(path / f"model_{t}.txt") for t in TASKS if (path / f"model_{t}.txt").exists()}
    return meta, models


def scan_graph_key(tif: str | Path) -> str:
    """Identifies the image network of a sheet: file name, size, loader version (not the folder it is in)."""
    from .scanroads import SCAN_LOADER_VERSION

    p = Path(tif)
    return f"{p.name}|{p.stat().st_size}|s{SCAN_LOADER_VERSION}"


def correct_graph(G: nx.DiGraph, tif: str, corrector: str | Path, graph_key: str | None = None) -> dict:
    """Correct an image network with a saved corrector. On the sheet it was trained on (same graph key), the
    out-of-fold predictions are used, so every block is corrected by a model that never saw its labels."""
    meta, models = load_corrector(corrector)
    graph_key = graph_key or scan_graph_key(tif)
    if graph_key and graph_key == meta.get("graph_key") and (Path(corrector) / "oof_predictions.json").exists():
        Pj = json.loads((Path(corrector) / "oof_predictions.json").read_text(encoding="utf-8"))
        node_type = type(next(iter(G.nodes)))
        P = {"roads": Pj["roads"], "crossings": {int(k): v for k, v in Pj["crossings"].items()},
             "crossing_nodes": [node_type(n) for n in Pj["crossing_nodes"]], "links": Pj.get("links") or []}
        mode = "out-of-fold (sheet the corrector was trained on: each block corrected by a model that never saw it)"
    else:
        P = predictions_from_models(G, tif, models, cnn_dir=corrector)
        mode = "models trained on " + str(meta.get("trained_on"))
    enabled = set(meta.get("enabled", []))
    if not meta.get("allow_removal"):
        enabled -= REMOVALS  # priced, not removed (correctors saved before this rule listed removals as enabled)
    ch = apply_corrections(G, P, dict(meta.get("thresholds") or {}, uncertain_road=THRESH["uncertain_road"]), enabled)
    if "move_lines" in enabled:
        from . import scangeom

        gp = Path(corrector) / "geometry_oof_offsets.json"
        if mode.startswith("out-of-fold") and gp.exists():
            offsets = json.loads(gp.read_text(encoding="utf-8"))
        else:
            offsets = scangeom.predict_offsets(G, tif, Path(corrector) / "model_geometry.txt")
        ch.update(scangeom.move_lines(G, offsets))
    ch["mode"] = mode
    G.graph["scan_corrected"] = ch
    return ch


def markdown(cv: dict, imp: dict, changes: dict | None, ds) -> str:
    L = ["# Learned corrector of the image network", "",
         "Labels from BD TOPO and OSM, features from the scan and the image network only. The sheet is cut into "
         f"{ds['grid']} x {ds['grid']} blocks shared among 4 folds; every score below is on blocks the model never saw.", "",
         "| decision | examples | share positive | AUC | accuracy, model | accuracy, fixed rule | F1 model / rule |",
         "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in TASKS:
        s = cv["tasks"].get(t, {})
        if "auc" not in s:
            L.append(f"| {TASK_TEXT[t]} | {s.get('n', 0)} | - | - | - | - | {s.get('skipped', '')} |")
            continue
        L.append(f"| {TASK_TEXT[t]} | {s['n']} | {s['positives']:.0%} | {s['auc']:.3f} | {s['accuracy_model']:.1%} | "
                 f"{s['accuracy_rule']:.1%} | {s['f1_model']:.2f} / {s['f1_rule']:.2f} |")
    g = cv["tasks"].get("road_real", {}).get("gaps")
    if g:
        L += ["", f"Label gaps: {g['n']} joins, {g['real']:.0%} real; AUC on gaps alone {g['auc']:.3f}." if g.get("auc") else ""]
    L += ["", "## Corrections and the gate", "",
          f"A correction is switched on only when, on unseen blocks, it makes at least {GATE_MIN} changes, at least "
          f"{GATE_PRECISION:.0%} of them are right, and that is {GATE_LIFT:g} times the base rate (the share of the same "
          "candidates that needed the change).", "",
          "| correction | threshold | changes | right | base rate | lift | switched on |", "| --- | --- | --- | --- | --- | --- | --- |"]
    text = {"drop_gap": "remove a label-gap join (p real below)", "drop_road": "remove a road (p real below)",
            "white_to_rank4": "white road to rank 4 (p importance 4 at least)", "bridge_to_culvert": "bridge to culvert (p bridge below)",
            "separate_crossing": "split a crossing into a flyover (p at least)"}
    for k, d in (cv.get("decisions") or {}).items():
        km = f" ({d['km']:.0f} km)" if d.get("km") else ""
        L.append(f"| {text.get(k, k)} | {d['threshold']:g} | {d['changes']}{km} | {d['precision']:.0%} | {d['base']:.0%} | "
                 f"{d['lift']:.1f} | {'yes' if d['enabled'] else 'no'} |")
    L += ["", "Removals pass the gate but are not applied unless `scan-learn --allow-removal`: about one removal in five "
          "hits a real road and can cut a village off. Every road carries instead its probability of being real, which "
          "prices it in the exercise and in the planner: a label-gap join costs 1 + 3 (1 - p) times its time, another road "
          "1 + 3 (0.7 - p) times below p = 0.7.",
          "", "## Most useful features (share of the gain)", ""]
    for t, fi in imp.items():
        L.append(f"- {t}: " + ", ".join(f"{k} {v:.0%}" for k, v in fi.items()))
    if changes:
        L += ["", "## Corrections applied to the image network (out-of-fold)", "", "| change | count |", "| --- | --- |"]
        L += [f"| {k.replace('_', ' ')} | {v} |" for k, v in changes.items() if k != "mode"]
    return "\n".join(L) + "\n"


def jsonable(cv: dict) -> dict:
    out = {"folds_of_blocks": cv["folds_of_blocks"], "tasks": {}}
    for t, s in cv["tasks"].items():
        out["tasks"][t] = {k: v for k, v in s.items() if not k.startswith("_")}
    return out


# ---------------------------------------------------------------------------
# map checks: what to verify on each designated route before the move
# ---------------------------------------------------------------------------

CHECK_P = 0.7  # roads below this probability of being real are listed


def route_checks(G: nx.DiGraph, nodes: list) -> list[dict]:
    """Stretches of a route (node list) on the image network to check before the move: label-gap joins, roads
    the corrector doubts (p real < 0.7) and bridges read on the map. Consecutive sections of the same kind are
    merged; each stretch has its chainage from the start, length, position and lowest probability."""
    out: list[dict] = []
    km = 0.0
    for u, v in zip(nodes[:-1], nodes[1:]):
        if not G.has_edge(u, v):
            continue
        d = G.edges[u, v]
        L = float(d.get("length_m") or 0.0) / 1000.0
        kind = None
        p = d.get("scan_p_real")
        if d.get("source_id") == "scan":
            if d.get("scan_link"):
                kind = "link added by the corrector: the map cuts the road here (expressway, railway or label), check that it goes through"
            elif d.get("scan_gap"):
                kind = "label-gap join: the map is hidden by a label here, check that the road goes through"
            elif p is not None and p < CHECK_P:
                kind = "doubtful road: the corrector doubts this line is a road"
            elif d.get("bridge"):
                kind = "bridge read on the map: load class unknown, check it carries the column"
        if kind:
            pb = d.get("scan_p_bridge") if d.get("bridge") else None
            score = p if p is not None else (0.85 if d.get("scan_gap") else 1.0)
            last = out[-1] if out else None
            if last and last["kind"] == kind and abs(last["end_km"] - km) < 1e-6:
                last["end_km"] = km + L
                last["p"] = min(last["p"], score)
                last["coords"].append((G.nodes[v]["x"], G.nodes[v]["y"]))
            else:
                out.append({"kind": kind, "start_km": km, "end_km": km + L, "p": score, "p_bridge": pb,
                            "coords": [(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])]})
        km += L
    return out


def plan_checks(G: nx.DiGraph, coa, H=None) -> tuple[str, list[dict]]:
    """Map checks of every MSR and ASR of a COA, as Markdown and as rows (for GeoJSON). Empty on a network
    that was not read from an image."""
    if not any(d.get("source_id") == "scan" for _, _, d in G.edges(data=True)):
        return "", []
    rows: list[dict] = []
    routes = []
    for gid, r in sorted(coa.design.msr.items()):
        routes.append((gid, "MSR", coa.ref.name_of(r) or "MSR", r))
        for a in coa.design.asrs.get(gid, []):
            routes.append((gid, "ASR", coa.ref.name_of(a) or "ASR", a))
    L = [f"# Map checks before the move (COA {coa.label})", "",
         "The road network was read from the scanned map. These stretches of the designated routes are the ones to "
         "check (map reconnaissance, imagery or a patrol) before the columns move, lowest probability first within each "
         f"route: label-gap joins, roads the learned corrector doubts (probability of being real below {CHECK_P:g}) and "
         "bridges read on the map (load class unknown).", ""]
    seen = set()
    for gid, role, name, r in routes:
        if (name, role) in seen:
            continue
        seen.add((name, role))
        ch = route_checks(G, r.nodes)
        total = sum(c["end_km"] - c["start_km"] for c in ch)
        L += [f"## {name} ({role} of {gid}, {r.length_m / 1000:.1f} km): {len(ch)} stretches, {total:.1f} km to check", ""]
        if not ch:
            L += ["Nothing to check on the map.", ""]
            continue
        first = [c for c in ch if c["p"] < CHECK_P or c["kind"].startswith(("bridge", "link"))]
        rest = [c for c in ch if c not in first]
        for c in ch:
            rows.append(dict(c, route=name, role=role, group=gid, priority="first" if c in first else "if time allows"))
        if first:
            L += ["Check first:", "", "| km from start | length | what | probability real | position (lon, lat) |", "| --- | --- | --- | --- | --- |"]
            for c in sorted(first, key=lambda c: (c["p"], c["start_km"])):
                mid = c["coords"][len(c["coords"]) // 2]
                L.append(f"| {c['start_km']:.1f} | {1000 * (c['end_km'] - c['start_km']):.0f} m | {c['kind']} | "
                         f"{c['p']:.2f} | {mid[0]:.5f}, {mid[1]:.5f} |")
            L.append("")
        if rest:
            km_rest = sum(c["end_km"] - c["start_km"] for c in rest)
            worst = min(c["p"] for c in rest)
            L += [f"Then, if time allows: {len(rest)} label-gap joins the corrector believes real (probability {worst:.2f} "
                  f"and above, {1000 * km_rest:.0f} m in all), listed in map_checks.geojson.", ""]
        L.append("")
    return "\n".join(L) + "\n", rows


def write_plan_checks(G: nx.DiGraph, coa, out: Path) -> int:
    """Write map_checks.md and map_checks.geojson for a COA; returns the number of stretches."""
    md, rows = plan_checks(G, coa)
    if not md:
        return 0
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "map_checks.md").write_text(md, encoding="utf-8")
    feats = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [list(map(float, c)) for c in r["coords"]]},
              "properties": {k: v for k, v in r.items() if k != "coords"}} for r in rows]
    (out / "map_checks.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}), encoding="utf-8")
    return len(rows)

"""Point-to-point route exercises read from a PDF (exercise file of the hackathon: one "IN / OUT / REQUIREMENTS"
slide and one description slide per exercise) and solved on the road network read from the scan.

Reading (:func:`read_pdf`)
* PDF metadata (author, dates, producer) and the text of every page (pypdf); the map figure of each slide is saved.
* Points: every ``NAME (LAT : y / LONG : x)``: deployment-area corners ``DA1..DA4`` (a quadrilateral, in that order),
  departure ``PDn``, arrival ``PAn``, mandatory waypoints ``WPn``.
* Objective: "shortest" (length) or "fastest" (time). The description slide decides; the requirement bullets are used
  when it says nothing, and a disagreement between the two is logged.
* Waypoints: "in the order WP1, WP2..." (fixed order) or "without any defined order" / "no defined order" (free order).
* Round trip: "two strictly different routes" (outbound and return without a common road section).
* Convoy speed cap: "not exceeding N km/h".

Solving (:func:`solve`)
* Allowed network: roads of rank 5 or better (no tracks) whose whole geometry lies inside the deployment area, largest
  strongly connected part; points are snapped to its nearest junction.
* Time of a section: its legal speed (French rules, from what the map shows: 50 km/h in a built-up area read from the
  scan tint, 110 on an expressway, 80 elsewhere) capped at the convoy cap.
* Cost: length (shortest) or time (fastest), times the corrector's price of doubtful roads and label gaps
  (:func:`coa_net.scanlearn.uncertainty_penalty`); lengths and times reported are never priced.
* Free-order waypoints: every order is tried on a matrix of point-to-point costs (4 waypoints: 24 orders).
* Round trip: one leg, then the other leg with every section of the first one forbidden (both directions, and any
  section running within 20 m of it); both orders are tried and the cheaper pair kept.
"""
from __future__ import annotations

import itertools
import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import networkx as nx
import numpy as np

L93 = "EPSG:2154"
PT = re.compile(r"\b([A-Z]{2}\d+)\s*\(\s*LAT\s*:\s*(-?[\d.]+)\s*/\s*LONG\s*:\s*(-?[\d.]+)\s*\)", re.I)
URBAN_KMH, ROAD_KMH, EXPRESSWAY_KMH = 50.0, 80.0, 110.0


# ---------------------------------------------------------------------------------------------------------------- reading

@dataclass
class PdfExercise:
    number: int
    pages: list[int]
    points: dict[str, tuple[float, float]]  # name -> (lon, lat)
    area: list[str]
    start: str
    end: str
    waypoints: list[str]
    waypoint_order: str  # "fixed", "free" or "none"
    objective: str  # "length" or "time"
    round_trip_distinct: bool
    speed_cap_kmh: float
    legal_speed: bool
    requirements: list[str]
    outputs: list[str]
    description: str
    notes: list[str] = field(default_factory=list)


def _bullets(text: str, head: str, stops: tuple[str, ...]) -> list[str]:
    m = re.search(head + r"\s*\n(.*?)(?=\n(?:" + "|".join(stops) + r")\s*\n|\Z)", text, re.S)
    if not m:
        return []
    items, cur = [], ""
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("•"):
            if cur:
                items.append(cur)
            cur = line.lstrip("• ").strip()
        elif line and cur and (not re.fullmatch(r"[A-Z]{2}\d+", line) or cur.rstrip().endswith((",", "and"))):
            cur += " " + line
    if cur:
        items.append(cur)
    return items


def read_pdf(pdf: str | Path, figures_dir: str | Path | None = None) -> tuple[dict, list[PdfExercise]]:
    """Metadata and exercises of the PDF; the map figure of each exercise slide goes to ``figures_dir``."""
    import pypdf

    r = pypdf.PdfReader(str(pdf))
    meta = {k.lstrip("/"): str(v) for k, v in (r.metadata or {}).items()}
    meta["pages"] = len(r.pages)
    by_ex: dict[int, list[tuple[int, str]]] = {}
    for i, p in enumerate(r.pages, start=1):
        t = p.extract_text() or ""
        m = re.search(r"EXERCISE\s*N\s*°\s*(\d+)", t, re.I)
        if m:
            by_ex.setdefault(int(m.group(1)), []).append((i, t))
            if figures_dir is not None and p.images:
                d = Path(figures_dir) / f"exercise_{int(m.group(1)):02d}"
                d.mkdir(parents=True, exist_ok=True)
                img = max(p.images, key=lambda im: len(im.data))
                try:
                    img.image.convert("RGB").save(d / f"pdf_page{i:02d}_figure.png")
                except Exception:  # codec missing: keep the raw image
                    (d / f"pdf_page{i:02d}_figure{Path(img.name).suffix or '.bin'}").write_bytes(img.data)
    out = []
    for n in sorted(by_ex):
        pages = by_ex[n]
        spec_text = next((t for _, t in pages if re.search(r"\bREQUIREMENTS\b", t)), pages[0][1])
        desc = " ".join(t for _, t in pages if t is not spec_text)
        desc = re.sub(r"\s+", " ", re.sub(r"EXERCISE\s*N\s*°\s*\d+", "", desc)).strip()
        pts = {m.group(1).upper(): (float(m.group(3)), float(m.group(2))) for m in PT.finditer(spec_text)}
        reqs = _bullets(spec_text, "REQUIREMENTS", ("OUT", "IN", "DA1"))
        outs = _bullets(spec_text, "OUT", ("REQUIREMENTS", "IN", "DA1"))
        notes = []
        area = sorted([k for k in pts if k.startswith("DA")], key=lambda k: int(k[2:]))
        start = next(k for k in pts if k.startswith("PD"))
        end = next(k for k in pts if k.startswith("PA"))
        wps = sorted([k for k in pts if k.startswith("WP")], key=lambda k: int(k[2:]))
        req_txt, low_desc = " ".join(reqs).lower(), desc.lower()
        mo = max(re.finditer(r"in the order ((?:WP\d+[,\s]*(?:and\s*)?)+)", " ".join(reqs) + " " + desc, re.I),
                 key=lambda m: len(re.findall(r"WP\d+", m.group(1))), default=None)
        if mo:
            order, wps = "fixed", re.findall(r"WP\d+", mo.group(1).upper())
        elif wps:
            order = "free"
            if not re.search(r"(without any|no) defined order", req_txt + " " + low_desc):
                notes.append("waypoints without an order statement: any order is allowed")
        else:
            order = "none"
        obj_desc = "time" if "fastest" in low_desc else "length" if "shortest" in low_desc else None
        obj_req = "time" if "as fast as possible" in req_txt else "length" if ("as short as possible" in req_txt or "shortest" in req_txt) else None
        objective = obj_desc or obj_req or "length"
        if obj_desc and obj_req and obj_desc != obj_req:
            notes.append(f"objective: the requirement bullets say '{'as fast as possible' if obj_req == 'time' else 'as short as possible'}' "
                         f"but the description slide asks for the {'fastest' if obj_desc == 'time' else 'shortest'} route; "
                         f"the description is followed ({'fastest' if objective == 'time' else 'shortest'})")
        cap = re.search(r"not exceeding\s*(\d+)\s*km/h", spec_text + " " + desc, re.I)
        out.append(PdfExercise(
            number=n, pages=[p for p, _ in pages], points=pts, area=area, start=start, end=end, waypoints=wps,
            waypoint_order=order, objective=objective,
            round_trip_distinct=bool(re.search(r"strictly different", req_txt + " " + low_desc)),
            speed_cap_kmh=float(cap.group(1)) if cap else 80.0,
            legal_speed=bool(re.search(r"legal speed limits|speed limits", spec_text + " " + desc, re.I)),
            requirements=reqs, outputs=outs, description=desc, notes=notes))
    return meta, out


# ---------------------------------------------------------------------------------------------------------------- network

def legal_speed_kmh(d: dict) -> float:
    """French default limit outside built-up areas, from what the map shows: expressway 110, any other road 80
    (inside a built-up area the limit is 50: see :attr:`Network.urban_share`)."""
    if d.get("highway") in ("motorway", "trunk", "motorway_link", "trunk_link") or d.get("scan_symbol") == "blue":
        return EXPRESSWAY_KMH
    return ROAD_KMH


class Network:
    """A road graph as arrays: edges, Lambert-93 lines, legal speeds, prices."""

    def __init__(self, G: nx.DiGraph, name: str, max_rank: int = 5, priced: bool = True, builtup=None):
        import shapely
        from pyproj import Transformer

        from .exercise import edge_lines
        from .scanlearn import uncertainty_penalty

        self.G, self.name = G, name
        self.edges = [(a, b) for a, b, d in G.edges(data=True) if d.get("road_rank", 5) <= max_rank]
        self.nodes = list(G.nodes)
        self.idx = {n: i for i, n in enumerate(self.nodes)}
        self.u = np.array([self.idx[a] for a, _ in self.edges])
        self.v = np.array([self.idx[b] for _, b in self.edges])
        D = [G.edges[e] for e in self.edges]
        self.length_m = np.array([d["length_m"] for d in D], float)
        self.speed = np.array([legal_speed_kmh(d) for d in D], float)
        self.price = np.array([uncertainty_penalty(d) if priced and d.get("source_id") == "scan" else 1.0 for d in D])
        self.lines = edge_lines(G, self.edges)
        tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
        x, y = tr.transform(np.array([G.nodes[n]["x"] for n in self.nodes]), np.array([G.nodes[n]["y"] for n in self.nodes]))
        self.xy = np.column_stack([x, y])
        self.tree = shapely.STRtree(self.lines)
        # share of each section's length inside a built-up area (50 km/h there)
        self.urban_share = np.zeros(len(self.edges))
        if builtup is not None and len(builtup):
            self.urban_share = urban_share(self.lines, builtup)
        self.eidx = {(int(a), int(b)): i for i, (a, b) in enumerate(zip(self.u, self.v))}

    def edge_probabilities(self, P, transform, step_m: float = 20.0) -> tuple[np.ndarray, np.ndarray]:
        """Road probability of a segmentation raster (``P[..., 0]``, x 255) sampled every ``step_m`` along every section:
        (owner section index, p). Max over 3 x 3 pixels, as the centre line and the road pixels can sit a pixel apart."""
        if getattr(self, "_prob", None) is None:
            import shapely
            from pyproj import Transformer

            L = shapely.length(self.lines)
            n = np.maximum(np.ceil(L / step_m).astype(int), 1)
            owner = np.repeat(np.arange(len(self.lines)), n)
            frac = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
            xy = shapely.get_coordinates(shapely.line_interpolate_point(self.lines[owner], frac, normalized=True))
            lon, lat = Transformer.from_crs(L93, "EPSG:4326", always_xy=True).transform(xy[:, 0], xy[:, 1])
            c = ((np.asarray(lon) - transform.c) / transform.a).astype(int)
            r = ((np.asarray(lat) - transform.f) / transform.e).astype(int)
            p = np.zeros(len(r))
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr = np.clip(r + dr, 0, P.shape[0] - 1)
                    cc = np.clip(c + dc, 0, P.shape[1] - 1)
                    p = np.maximum(p, P[rr, cc, 0] / 255.0)
            self._prob = (owner, p)
        return self._prob

    def price_by_probability(self, P, transform, threshold: float, strength: float = 3.0) -> dict:
        """Penalise the sections the segmentation network is unsure of: cost x (1 + strength x share of the section whose
        road probability is below ``threshold``). The threshold is the operator's choice: the higher it is, the more of the
        network counts as doubtful. Returns what was priced."""
        owner, p = self.edge_probabilities(P, transform)
        cnt = np.bincount(owner, minlength=len(self.edges))
        self.prob_share_below = np.bincount(owner, weights=(p < threshold).astype(float), minlength=len(self.edges)) / np.maximum(cnt, 1)
        self.prob_mean = np.bincount(owner, weights=p, minlength=len(self.edges)) / np.maximum(cnt, 1)
        self.price = self.price * (1.0 + strength * self.prob_share_below)
        self.prob_threshold, self.prob_strength = threshold, strength
        doubtful = self.prob_share_below > 0
        return {"threshold": threshold, "strength": strength, "sections_priced": int(doubtful.sum()),
                "km_priced": round(float(self.length_m[doubtful].sum()) / 2000, 1),  # both directions are sections
                "km_below_threshold": round(float((self.length_m * self.prob_share_below).sum()) / 2000, 1)}

    def price_by_confidence(self, owner: np.ndarray, p_cal: np.ndarray, strength: float = 1.0, p_ref: float = 0.75,
                            floor: float = 0.05, exempt: np.ndarray | None = None) -> dict:
        """Uncertainty-aware cost from the segmentation network (TIFF-only routing).

        Every section gets a *risk* r = mean over its 20 m samples of -ln(c), with c = min(p_cal / p_ref, 1) the calibrated
        road probability normalised by the plateau the calibration reaches on certain roads (``p_ref``, about 0.75 with the
        3 px label band), floored at ``floor``. r is 0 on a road the model is sure of, 0.69 where it is half as sure, 3.0 at
        the floor; exp(-r) is the section's confidence (geometric mean of c). The cost becomes base x (1 + strength x r):
        a route pays ``strength`` extra metres (or minutes) per metre and per nat of surprise, i.e. it trades length for
        the log-likelihood that the roads it uses exist. ``exempt`` sections (roads added from another source) keep r = 0."""
        cnt = np.maximum(np.bincount(owner, minlength=len(self.edges)), 1)
        c = np.clip(np.asarray(p_cal, float) / max(p_ref, 1e-6), floor, 1.0)
        risk = np.bincount(owner, weights=-np.log(c), minlength=len(self.edges)) / cnt
        if exempt is not None:
            risk[exempt] = 0.0
        self.risk, self.confidence = risk, np.exp(-risk)
        self.conf_strength, self.conf_ref = strength, p_ref
        self.price = self.price * (1.0 + strength * risk)
        low = self.confidence < 0.5
        return {"strength": strength, "p_ref": round(float(p_ref), 3), "floor": floor,
                "km_low_confidence": round(float(self.length_m[low].sum()) / 2000, 1),  # both directions are sections
                "network_mean_confidence": round(float(np.average(self.confidence, weights=self.length_m)), 3)}

    def time_min(self, cap: float) -> np.ndarray:
        """Built-up part at min(50, cap), the rest at min(limit, cap)."""
        km = self.length_m / 1000.0
        return (km * self.urban_share / min(URBAN_KMH, cap) + km * (1 - self.urban_share) / np.minimum(self.speed, cap)) * 60.0


def urban_share(lines, builtup, step_m: float = 20.0, px_m: float = 10.0) -> np.ndarray:
    """Share of each Lambert-93 line inside the built-up polygons: the polygons rasterized at ``px_m``, the lines sampled
    every ``step_m``."""
    import shapely
    from rasterio.features import rasterize
    from rasterio.transform import from_origin

    bu = builtup.to_crs(L93)
    x0, y0, x1, y1 = bu.total_bounds
    W, H = int((x1 - x0) / px_m) + 2, int((y1 - y0) / px_m) + 2
    tr = from_origin(x0 - px_m, y1 + px_m, px_m, px_m)
    mask = rasterize([(g, 1) for g in bu.geometry.values], out_shape=(H + 2, W + 2), transform=tr, fill=0, dtype="uint8")
    L = shapely.length(lines)
    n = np.maximum(np.ceil(L / step_m).astype(int), 1)
    owner = np.repeat(np.arange(len(lines)), n)
    frac = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
    pts = shapely.get_coordinates(shapely.line_interpolate_point(lines[owner], frac, normalized=True))
    c = ((pts[:, 0] - tr.c) / px_m).astype(int)
    r = ((tr.f - pts[:, 1]) / px_m).astype(int)
    ok = (r >= 0) & (r < mask.shape[0]) & (c >= 0) & (c < mask.shape[1])
    v = np.zeros(len(pts))
    v[ok] = mask[r[ok], c[ok]]
    return np.bincount(owner, weights=v, minlength=len(lines)) / n


def to_l93(lonlat):
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    a = np.asarray(lonlat, float).reshape(-1, 2)
    return np.column_stack(tr.transform(a[:, 0], a[:, 1]))


# ---------------------------------------------------------------------------------------------------------------- solving

class Solver:
    def __init__(self, N: Network, allowed: np.ndarray, cost: np.ndarray):
        from scipy.sparse import csr_matrix
        from scipy.sparse.csgraph import connected_components

        self.N, self.allowed, self.cost = N, allowed.copy(), cost
        n = len(N.nodes)
        e = np.flatnonzero(allowed)
        A = csr_matrix((np.ones(len(e)), (N.u[e], N.v[e])), shape=(n, n))
        _, lab = connected_components(A, directed=True, connection="strong")
        used = np.zeros(n, bool)
        used[N.u[e]] = used[N.v[e]] = True
        big = np.bincount(lab[used]).argmax() if used.any() else 0
        self.in_big = used & (lab == big)
        from scipy.spatial import cKDTree

        self.cands = np.flatnonzero(self.in_big)
        self.kd = cKDTree(N.xy[self.cands])

    def snap(self, xy) -> tuple[int, float]:
        d, j = self.kd.query(xy)
        return int(self.cands[j]), float(d)

    def matrix(self, forbid: np.ndarray | None = None):
        from scipy.sparse import csr_matrix

        m = self.allowed if forbid is None else (self.allowed & ~forbid)
        e = np.flatnonzero(m)
        n = len(self.N.nodes)
        return csr_matrix((np.maximum(self.cost[e], 1e-9), (self.N.u[e], self.N.v[e])), shape=(n, n))

    def paths_from(self, A, src: int):
        from scipy.sparse.csgraph import dijkstra

        return dijkstra(A, directed=True, indices=src, return_predecessors=True)

    def edges_of(self, pred, src: int, dst: int) -> list[int] | None:
        out, x = [], dst
        while x != src:
            p = pred[x]
            if p < 0:
                return None
            out.append(self.N.eidx[(int(p), int(x))])
            x = p
        return out[::-1]

    def route(self, nodes: list[int], forbid=None) -> tuple[list[int], float] | None:
        """Cheapest route through ``nodes`` in that order."""
        A = self.matrix(forbid)
        edges, tot = [], 0.0
        for a, b in zip(nodes[:-1], nodes[1:]):
            d, pred = self.paths_from(A, a)
            if not np.isfinite(d[b]):
                return None
            edges += self.edges_of(pred, a, b)
            tot += float(d[b])
        return edges, tot

    def best_order(self, start: int, wps: list[int], end: int) -> tuple[list[int], list[tuple[int, ...]], float]:
        """Free-order waypoints: costs between every pair of points, every order tried."""
        A = self.matrix()
        pts = [start] + wps + [end]
        D = np.full((len(pts), len(pts)), np.inf)
        for i, p in enumerate(pts[:-1]):
            d, _ = self.paths_from(A, p)
            D[i] = d[pts]
        ranking = []
        for perm in itertools.permutations(range(1, len(wps) + 1)):
            seq = (0,) + perm + (len(pts) - 1,)
            ranking.append((float(sum(D[a, b] for a, b in zip(seq[:-1], seq[1:]))), perm))
        ranking.sort()
        return list(ranking[0][1]), [p for _, p in ranking], ranking[0][0], ranking

    def overlap_mask(self, edges: list[int], dist_m: float = 20.0, share: float = 0.5) -> np.ndarray:
        """Sections used by ``edges`` (both directions) and any section running within ``dist_m`` of them on more than
        ``share`` of its length."""
        import shapely

        N = self.N
        m = np.zeros(len(N.edges), bool)
        used = set(edges)
        for i in used:
            m[i] = True
            j = N.eidx.get((int(N.v[i]), int(N.u[i])))
            if j is not None:
                m[j] = True
        buf = shapely.union_all(N.lines[list(used)]).buffer(dist_m)
        near = N.tree.query(buf, predicate="intersects")
        if len(near):
            inside = shapely.length(shapely.intersection(N.lines[near], buf)) / np.maximum(shapely.length(N.lines[near]), 1e-6)
            m[near[inside > share]] = True
        return m


# ---------------------------------------------------------------------------------------------------------------- facts

def route_facts(N: Network, edges: list[int], cap: float, bd_lines=None, osm=None) -> dict:
    import shapely

    G = N.G
    t = N.time_min(cap)
    e = np.array(edges, int)
    D = [G.edges[N.edges[i]] for i in edges]
    sym: dict[str, float] = {}
    for d, L in zip(D, N.length_m[e]):
        k = d.get("scan_symbol") or d.get("highway") or "?"
        sym[k] = sym.get(k, 0.0) + L / 1000
    doubtful = [(d, L) for d, L in zip(D, N.length_m[e]) if d.get("scan_p_real") is not None and d["scan_p_real"] < 0.7]
    names = []
    for d in D:
        nm = d.get("name") or d.get("ref") or ""
        if nm and nm not in names:
            names.append(nm)
    cont = all(int(N.v[a]) == int(N.u[b]) for a, b in zip(e[:-1], e[1:]))
    f = {
        "length_km": round(float(N.length_m[e].sum()) / 1000, 2),
        "duration_min": round(float(t[e].sum()), 1),
        "mean_speed_kmh": round(float(N.length_m[e].sum()) / 1000 / max(float(t[e].sum()), 1e-9) * 60, 1),
        "sections": len(edges),
        "continuous": bool(cont),
        "km_by_speed_limit": _km_by_limit(N, e, cap),
        "km_by_map_symbol": {k: round(v, 2) for k, v in sorted(sym.items(), key=lambda kv: -kv[1])},
        "km_built_up": round(float((N.length_m[e] * N.urban_share[e]).sum()) / 1000, 2),
        "doubtful_sections": {"count": len(doubtful), "km": round(sum(L for _, L in doubtful) / 1000, 2)},
        "links_added_by_corrector_used": sum(1 for d in D if d.get("scan_link")),
        **({"km_below_prob_threshold": round(float((N.length_m[e] * N.prob_share_below[e]).sum()) / 1000, 2),
            "mean_prob": round(float(np.average(N.prob_mean[e], weights=N.length_m[e])), 3)}
           if getattr(N, "prob_share_below", None) is not None else {}),
        **({"mean_confidence": round(float(np.exp(-np.average(N.risk[e], weights=N.length_m[e]))), 3),
            "km_low_confidence": round(float(N.length_m[e][N.confidence[e] < 0.5].sum()) / 1000, 2),
            "uncertainty_cost_share": round(float((N.length_m[e] * N.conf_strength * N.risk[e]).sum()
                                                  / max((N.length_m[e] * (1 + N.conf_strength * N.risk[e])).sum(), 1e-9)), 3)}
           if getattr(N, "risk", None) is not None else {}),
        "label_gap_joins_used": sum(1 for d in D if d.get("scan_gap") and not d.get("scan_link")),
        "road_names": names[:12],
    }
    # the route in driving order: the sections' coordinates chained (a merge of the union would split where the route
    # passes a junction twice)
    xy = np.vstack([shapely.get_coordinates(g) for g in N.lines[e]])
    keep = np.ones(len(xy), bool)
    keep[1:] = np.any(np.abs(np.diff(xy, axis=0)) > 1e-6, axis=1)
    line = shapely.LineString(xy[keep])
    if bd_lines is not None:
        pts = _sample(N.lines[e], 25.0)
        if len(pts):
            d, _ = bd_lines.query(pts)
            f["share_within_25m_of_bdtopo"] = round(float(np.mean(d <= 25.0)), 3)
    if osm is not None:
        f["osm_speed_check"] = osm_duration(N.lines[e], N.speed[e], cap, osm, N.urban_share[e])
    f["_line"] = line
    return f


def _km_by_limit(N: Network, e: np.ndarray, cap: float) -> dict:
    out = {f"{int(URBAN_KMH)} km/h (built-up, driven at {int(min(URBAN_KMH, cap))})": float((N.length_m[e] * N.urban_share[e]).sum()) / 1000}
    rest = N.length_m[e] * (1 - N.urban_share[e])
    for s in sorted(set(N.speed[e].tolist())):
        out[f"{int(s)} km/h (driven at {int(min(s, cap))})"] = float(rest[N.speed[e] == s].sum()) / 1000
    return {k: round(v, 2) for k, v in out.items() if v > 0.005}


def _sample(lines, step):
    import shapely

    out = []
    for g in lines:
        L = shapely.length(g)
        k = max(int(L // step), 1)
        out.append(shapely.get_coordinates(shapely.line_interpolate_point(g, np.linspace(0, L, k + 1))))
    return np.vstack(out) if out else np.zeros((0, 2))


def osm_duration(lines, speeds, cap, osm, urban=None) -> dict:
    """Duration with OSM ``maxspeed`` where a tagged OSM road runs within 20 m (sampled every 50 m), the map rule elsewhere."""
    import shapely

    tree, ms = osm
    tot, tagged_m, all_m = 0.0, 0.0, 0.0
    urban = np.zeros(len(speeds)) if urban is None else urban
    for g, s, us in zip(lines, speeds, urban):
        s = s if us < 0.5 else min(s, URBAN_KMH)
        L = float(shapely.length(g))
        k = max(int(L // 50), 1)
        pts = shapely.line_interpolate_point(g, (np.arange(k) + 0.5) * L / k)
        for p in pts:
            j = tree.query(p, predicate="dwithin", distance=20.0)
            v = [ms[i] for i in j if ms[i] > 0]
            sp = min(min(v), cap) if v else min(s, cap)
            tagged_m += L / k if v else 0.0
            tot += L / k / 1000 / sp * 60
        all_m += L
    return {"duration_min": round(tot, 1), "share_of_route_with_osm_maxspeed": round(tagged_m / max(all_m, 1e-9), 3)}


# ---------------------------------------------------------------------------------------------------------------- one exercise

def solve(ex: PdfExercise, N: Network, bd_lines=None, osm=None, avoid=None, forbid: np.ndarray | None = None) -> dict:
    """``avoid``: polygons (lon/lat coordinate lists) that no road section may cross (zones to avoid, RA).
    ``forbid``: boolean mask of sections removed from the network (stress tests: missing data)."""
    import shapely

    cap = ex.speed_cap_kmh
    area = shapely.Polygon(to_l93([ex.points[k] for k in ex.area]))
    inside = shapely.contains(area, N.lines)
    for poly in avoid or []:
        ra = shapely.Polygon(to_l93(poly))
        if ra.is_valid and not ra.is_empty:
            inside &= ~shapely.intersects(ra, N.lines)
    if forbid is not None:
        inside &= ~forbid
    t = N.time_min(cap)
    base = N.length_m if ex.objective == "length" else t
    S = Solver(N, inside, base * N.price)
    S0 = Solver(N, inside, base)  # same network without the corrector's prices, for the record
    names = [ex.start] + ex.waypoints + [ex.end]
    snapped = {}
    for k in names:
        xy = to_l93([ex.points[k]])[0]
        n, d = S.snap(xy)
        snapped[k] = {"node": n, "snap_m": round(d, 1), "inside_area": bool(shapely.contains(area, shapely.Point(xy)))}
    log = []
    res = {"network": N.name, "objective": ex.objective, "speed_cap_kmh": cap, "points": snapped,
           "allowed_sections": int(inside.sum()), "sections_outside_area_excluded": int((~inside).sum()), "legs": [], "log": log}
    nd = lambda k: snapped[k]["node"]
    if ex.round_trip_distinct:
        best = None
        # strictly different first; if a point is reached by a single road, the two legs may share sections only within
        # `relax` m of the departure and arrival points (reported)
        ends = N.xy[[nd(ex.start), nd(ex.end)]]
        near_end = lambda r: (np.minimum(np.hypot(*(N.xy[N.u] - ends[0]).T), np.hypot(*(N.xy[N.u] - ends[1]).T)) <= r) & \
                             (np.minimum(np.hypot(*(N.xy[N.v] - ends[0]).T), np.hypot(*(N.xy[N.v] - ends[1]).T)) <= r)
        relax_used = 0
        for relax in (0, 500, 1000, 2000, 5000):
            for first in ("out", "back"):
                a, b = (ex.start, ex.end) if first == "out" else (ex.end, ex.start)
                r1 = S.route([nd(a), nd(b)])
                if r1 is None:
                    continue
                forbid = S.overlap_mask(r1[0])
                if relax:
                    forbid &= ~near_end(relax)
                r2 = S.route([nd(b), nd(a)], forbid=forbid)
                if r2 is None:
                    log.append(f"{first} leg first: no return without a common section"
                               + (f" outside {relax} m of the departure and arrival points" if relax else ""))
                    continue
                tot = r1[1] + r2[1]
                log.append(f"{'outbound' if first == 'out' else 'return'} leg first, then the other leg with its sections forbidden: "
                           f"total cost {tot:.1f} {'m' if ex.objective == 'length' else 'min'} (priced)")
                if best is None or tot < best[0]:
                    legs = [(f"{ex.start} -> {ex.end} (outbound)", r1[0]), (f"{ex.end} -> {ex.start} (return)", r2[0])] if first == "out" \
                        else [(f"{ex.start} -> {ex.end} (outbound)", r2[0]), (f"{ex.end} -> {ex.start} (return)", r1[0])]
                    best = (tot, legs, first)
            if best is not None:
                relax_used = relax
                break
        if best is None:
            raise RuntimeError("no pair of different routes, even sharing up to 5 km around the departure and arrival points")
        res["shared_allowed_near_points_m"] = relax_used
        if relax_used:
            log.append(f"WARNING: no strictly different pair on this network: the legs may share sections within {relax_used} m of "
                       f"{ex.start} and {ex.end} (a point reached by a single road)")
        log.append(f"kept: {'outbound' if best[2] == 'out' else 'return'} leg solved first (cheaper total)")
        legs = best[1]
        shared = S.overlap_mask(legs[0][1])[legs[1][1]]
        res["common_sections_between_legs"] = int(shared.sum())
        res["common_km_between_legs"] = round(float(N.length_m[np.array(legs[1][1])][shared].sum()) / 1000, 2)
        ref_out, ref_back = S0.route([nd(ex.start), nd(ex.end)]), S0.route([nd(ex.end), nd(ex.start)])
        res["unconstrained_reference"] = {"outbound_" + ("km" if ex.objective == "length" else "min"): round(ref_out[1] / (1000 if ex.objective == "length" else 1), 2),
                                          "return_" + ("km" if ex.objective == "length" else "min"): round(ref_back[1] / (1000 if ex.objective == "length" else 1), 2)}
    else:
        order = ex.waypoints
        if ex.waypoint_order == "free" and ex.waypoints:
            perm, _, cost, ranking = S.best_order(nd(ex.start), [nd(w) for w in ex.waypoints], nd(ex.end))
            order = [ex.waypoints[i - 1] for i in perm]
            res["orders_tried"] = len(ranking)
            res["order_ranking"] = [{"order": [ex.waypoints[i - 1] for i in p], "cost": round(c / (1000 if ex.objective == "length" else 1), 2)}
                                    for c, p in ranking[:5]]
            log.append(f"free-order waypoints: {len(ranking)} orders tried on a matrix of point-to-point costs; best "
                       f"{' -> '.join([ex.start] + order + [ex.end])}")
        seq = [ex.start] + order + [ex.end]
        r = S.route([nd(k) for k in seq])
        if r is None:
            raise RuntimeError("no route")
        unit = "km" if ex.objective == "length" else "min"
        log.append(f"cheapest {'length' if ex.objective == 'length' else 'time'} route through {' -> '.join(seq)} on the allowed network "
                   f"(Dijkstra per leg): priced cost {r[1] / (1000 if ex.objective == 'length' else 1):.2f} {unit}")
        legs = [(" -> ".join(seq), r[0])]
        res["visit_order"] = seq
        r0 = S0.route([nd(k) for k in seq])
        res["unpriced_optimum"] = {"length_km": round(float(N.length_m[r0[0]].sum()) / 1000, 2),
                                   "duration_min": round(float(t[r0[0]].sum()), 1), "same_route": sorted(r0[0]) == sorted(r[0])}
    for k, v in snapped.items():
        log.insert(0, f"{k} snapped to the nearest junction of the allowed network, {v['snap_m']:.0f} m away"
                   + ("" if v["inside_area"] else " (WARNING: the point itself is outside the deployment area)"))
    res["_solver"], res["_solver_unpriced"], res["_leg_edges"] = S, S0, [list(edges) for _, edges in legs]
    for name, edges in legs:
        f = route_facts(N, edges, cap, bd_lines, osm)
        f["inside_area"] = bool(inside[edges].all())
        res["legs"].append({"name": name, **f})
    res["total"] = {"length_km": round(sum(l["length_km"] for l in res["legs"]), 2),
                    "duration_min": round(sum(l["duration_min"] for l in res["legs"]), 1)}
    return res


# ---------------------------------------------------------------------------------------------------------------- outputs

def write_geojson(path: Path, ex: PdfExercise, res: dict) -> None:
    import geopandas as gpd
    import shapely

    rows, geoms = [], []
    for leg in res["legs"]:
        rows.append({"kind": "route", "name": leg["name"], "length_km": leg["length_km"], "duration_min": leg["duration_min"],
                     "objective": ex.objective, "network": res["network"]})
        geoms.append(leg["_line"])
    for k, (x, y) in ex.points.items():
        rows.append({"kind": "deployment area corner" if k.startswith("DA") else "point", "name": k})
        geoms.append(shapely.Point(to_l93([(x, y)])[0]))
    rows.append({"kind": "deployment area", "name": "-".join(ex.area)})
    geoms.append(shapely.Polygon(to_l93([ex.points[k] for k in ex.area])))
    gpd.GeoDataFrame(rows, geometry=geoms, crs=L93).to_crs("EPSG:4326").to_file(path, driver="GeoJSON")


def render_layout(path: Path, tif: str, ex: PdfExercise, res: dict, ref: dict | None = None) -> None:
    """Layout: the scan around the route, the deployment area, the points and the route(s) with their length and time."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    to_ll = Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    lls = [np.column_stack(to_ll.transform(*shapely.get_coordinates(l["_line"]).T)) for l in res["legs"]]
    allp = np.vstack(lls + [np.array([ex.points[k] for k in ex.points if not k.startswith("DA")])])
    x0, y0 = allp.min(0) - 0.04
    x1, y1 = allp.max(0) + 0.04
    # keep a readable shape: at least 0.25 x 0.15 degrees
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = max((x1 - x0) / 2, 0.125), max((y1 - y0) / 2, 0.075)
    x0, x1, y0, y1 = cx - hw, cx + hw, cy - hh, cy + hh
    with rasterio.open(tif) as src:
        w = from_bounds(x0, y0, x1, y1, src.transform).round_offsets().round_lengths()
        f = max(1, int(max(w.width, w.height) // 3000))
        a = src.read(1, window=w, out_shape=(int(w.height // f), int(w.width // f)), resampling=Resampling.nearest, boundless=True)
        cm = src.colormap(1)
        lut = np.zeros((256, 3), np.uint8)
        for k, v in cm.items():
            lut[k] = v[:3]
        b = rasterio.windows.bounds(w, src.transform)
    fig, ax = plt.subplots(figsize=(16, 16 * (y1 - y0) / (x1 - x0) / math.cos(math.radians((y0 + y1) / 2)) + 1.5), dpi=110)
    ax.imshow(lut[a], extent=(b[0], b[2], b[1], b[3]), alpha=0.75)
    da = np.array([ex.points[k] for k in ex.area] + [ex.points[ex.area[0]]])
    ax.plot(da[:, 0], da[:, 1], "--", color="black", lw=2, label="deployment area")
    colours = ["#d7191c", "#2b83ba", "#1a9641"]
    for i, (ll, leg) in enumerate(zip(lls, res["legs"])):
        ax.plot(ll[:, 0], ll[:, 1], color=colours[i % 3], lw=4 if i == 0 else 3, alpha=0.9,
                label=f"{leg['name']}: {leg['length_km']:.1f} km, {leg['duration_min']:.0f} min")
    if ref:
        for leg in ref["legs"]:
            ll = np.column_stack(to_ll.transform(*shapely.get_coordinates(leg["_line"]).T))
            ax.plot(ll[:, 0], ll[:, 1], ":", color="black", lw=1.5, alpha=0.8)
        ax.plot([], [], ":", color="black", label=f"same exercise on BD TOPO (check): {ref['total']['length_km']:.1f} km, {ref['total']['duration_min']:.0f} min")
    for k, (x, y) in ex.points.items():
        c = {"PD": "#008000", "PA": "#b00000", "WP": "#ff8c00", "DA": "black"}[k[:2]]
        ax.plot(x, y, "o" if k[:2] != "DA" else "s", color=c, ms=11, mec="white", mew=2, zorder=5)
        lab = k
        if "visit_order" in res and k in res["visit_order"][1:-1]:
            lab += f" (#{res['visit_order'].index(k)})"
        if x0 <= x <= x1 and y0 <= y <= y1:
            ax.annotate(lab, (x, y), xytext=(7, 7), textcoords="offset points", fontsize=12, weight="bold", color=c,
                        bbox=dict(boxstyle="round,pad=0.2", fc="white", alpha=0.8))
    # overview: the whole deployment area, the route(s) and the frame of the main map
    ins = ax.inset_axes([0.74, 0.70, 0.25, 0.28])
    ins.plot(da[:, 0], da[:, 1], "--", color="black", lw=1)
    for i, ll in enumerate(lls):
        ins.plot(ll[:, 0], ll[:, 1], color=colours[i % 3], lw=1.5)
    ins.plot([x0, x1, x1, x0, x0], [y0, y0, y1, y1, y0], color="grey", lw=1)
    for k, (x, y) in ex.points.items():
        if not k.startswith("DA"):
            ins.plot(x, y, "o", color={"PD": "#008000", "PA": "#b00000", "WP": "#ff8c00"}[k[:2]], ms=3)
    ins.set_aspect(1 / math.cos(math.radians((y0 + y1) / 2)))
    ins.set_xticks([]); ins.set_yticks([])
    ins.set_facecolor((1, 1, 1, 0.85))
    ins.set_title("deployment area", fontsize=9)
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect(1 / math.cos(math.radians((y0 + y1) / 2)))
    obj = "shortest" if ex.objective == "length" else "fastest"
    ax.set_title(f"Exercise {ex.number}: {obj} route{'s' if len(res['legs']) > 1 else ''} - total {res['total']['length_km']:.1f} km, "
                 f"{res['total']['duration_min']:.0f} min (legal limits, convoy <= {ex.speed_cap_kmh:.0f} km/h)", fontsize=14)
    ax.legend(loc="lower left", fontsize=11, framealpha=0.9)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def decision_markdown(ex: PdfExercise, res: dict, ref: dict | None, net_facts: dict) -> str:
    obj = "shortest (length)" if ex.objective == "length" else "fastest (time)"
    L = [f"# Exercise {ex.number}", "",
         f"Read from PDF pages {', '.join(map(str, ex.pages))}.", "", "## What the exercise asks", "",
         "**Requirements (PDF):**", ""] + [f"- {r}" for r in ex.requirements] + ["", "**Outputs asked (PDF):**", ""] + \
        [f"- {o}" for o in ex.outputs] + ["", f"**Description slide:** {ex.description}", "", "## How the pipeline read it", "",
                                         "| item | value |", "| --- | --- |",
                                         f"| deployment area | quadrilateral {' - '.join(ex.area)} (only road sections lying entirely inside are allowed) |",
                                         f"| start / end | {ex.start} ({ex.points[ex.start][1]:.5f} N, {ex.points[ex.start][0]:.5f} E) -> {ex.end} ({ex.points[ex.end][1]:.5f} N, {ex.points[ex.end][0]:.5f} E) |",
                                         f"| waypoints | {', '.join(ex.waypoints) or 'none'} ({ {'fixed': 'in this order', 'free': 'any order', 'none': '-'}[ex.waypoint_order]}) |",
                                         f"| objective | {obj} |",
                                         f"| round trip with two strictly different routes | {'yes' if ex.round_trip_distinct else 'no'} |",
                                         f"| speed | legal limit of each section, capped at {ex.speed_cap_kmh:.0f} km/h (convoy) |"]
    if ex.notes:
        L += ["", "**Interpretation notes:**", ""] + [f"- {n}" for n in ex.notes]
    L += ["", "## Constraints applied", "",
          f"- **Road network**: {net_facts['source']}; roads of rank 5 or better (tracks and paths excluded); "
          f"{res['allowed_sections']:,} sections inside the deployment area, {res['sections_outside_area_excluded']:,} outside excluded.",
          f"- **Learned corrector**: {net_facts['corrector']}",
          f"- **Continuity**: one connected chain of road sections (checked: {'yes' if all(l['continuous'] for l in res['legs']) else 'NO'}); "
          "points are snapped to the nearest junction of the largest strongly connected part of the allowed network.",
          f"- **Inside the deployment area**: every section of the route lies entirely inside the quadrilateral (checked: {'yes' if all(l['inside_area'] for l in res['legs']) else 'NO'}).",
          f"- **Speed limits**: the map gives no speed signs, so the French default limits are applied from what the map shows: "
          f"{URBAN_KMH:.0f} km/h on the part of each section inside a built-up area ({net_facts.get('builtup', 'read from the scan tint')}), {EXPRESSWAY_KMH:.0f} km/h on expressways (blue), "
          f"{ROAD_KMH:.0f} km/h on other roads; then capped at the convoy's {ex.speed_cap_kmh:.0f} km/h. "
          "Duration = sum over sections of length / min(limit, cap).",
          "- **Doubtful roads**: each road carries the corrector's probability of being real; the route choice prices a label-gap join "
          "1 + 3 (1 - p) times and a road below p = 0.7 1 + 3 (0.7 - p) times, so a route avoids roads the map may not have. "
          "The lengths and durations reported are the real ones (not priced)."]
    if net_facts.get("probability_penalty"):
        L.append(f"- **Uncertain roads**: {net_facts['probability_penalty']}.")
    if ex.round_trip_distinct:
        L.append("- **Strictly different routes**: the second leg may not use any section of the first leg (either direction) nor any "
                 "section running within 20 m of it on more than half of its length; both leg orders are solved and the cheaper pair is kept "
                 f"(common sections between the two legs: {res['common_sections_between_legs']}, {res.get('common_km_between_legs', 0):.1f} km"
                 + (f"; RELAXED: sharing allowed within {res['shared_allowed_near_points_m']} m of the departure and arrival points, "
                    "because one of them is reached by a single road on this network" if res.get("shared_allowed_near_points_m") else "") + ").")
    if ex.waypoint_order == "free":
        L.append(f"- **Waypoints in any order**: all {res['orders_tried']} orders are compared on a matrix of point-to-point costs.")
    if ex.waypoint_order == "fixed":
        L.append(f"- **Waypoints in a fixed order**: {' -> '.join(res['visit_order'])}.")
    L += ["", "## What the pipeline decided", ""] + [f"- {x}" for x in res["log"]]
    if "order_ranking" in res:
        unit = "km" if ex.objective == "length" else "min (priced)"
        L += ["", "| rank | order | cost |", "| --- | --- | --- |"] + \
             [f"| {i + 1} | {' -> '.join(o['order'])} | {o['cost']:.2f} {unit} |" for i, o in enumerate(res["order_ranking"])]
    L += ["", "## Result", "", "| leg | length | duration | mean speed | km at each limit | km in built-up areas | doubtful sections | corrector links used | within 25 m of BD TOPO | duration with OSM maxspeed |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for l in res["legs"]:
        L.append(f"| {l['name']} | **{l['length_km']:.2f} km** | **{l['duration_min']:.1f} min** | {l['mean_speed_kmh']:.0f} km/h | "
                 f"{', '.join(f'{v:.1f} km at {k}' for k, v in l['km_by_speed_limit'].items())} | {l['km_built_up']:.1f} | "
                 f"{l['doubtful_sections']['count']} ({l['doubtful_sections']['km']:.1f} km) | {l['links_added_by_corrector_used']} | "
                 f"{l.get('share_within_25m_of_bdtopo', float('nan')):.0%} | " +
                 (f"{l['osm_speed_check']['duration_min']:.1f} min ({l['osm_speed_check']['share_of_route_with_osm_maxspeed']:.0%} of the route tagged) |"
                  if "osm_speed_check" in l else "not checked (no OSM) |"))
    if len(res["legs"]) > 1:
        L.append(f"| **total** | **{res['total']['length_km']:.2f} km** | **{res['total']['duration_min']:.1f} min** | | | | | | | |")
    L += ["", "Map symbols used (km): " + "; ".join(f"{l['name']}: " + ", ".join(f"{k} {v:.1f}" for k, v in l["km_by_map_symbol"].items()) for l in res["legs"])]
    sn = res["points"]
    L += ["", "Snapping of the points to the network: " + ", ".join(f"{k} {v['snap_m']:.0f} m" for k, v in sn.items()) + "."]
    if "unpriced_optimum" in res:
        u = res["unpriced_optimum"]
        L += ["", f"Without the corrector's prices the {('shortest' if ex.objective == 'length' else 'fastest')} route would be "
                  f"{u['length_km']:.2f} km / {u['duration_min']:.1f} min ({'the same route' if u['same_route'] else 'a different route, through roads the corrector doubts'})."]
    if "unconstrained_reference" in res:
        L += ["", "Each leg alone, without the strictly-different constraint and without prices: " +
              ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in res["unconstrained_reference"].items()) + "."]
    if ref:
        L += ["", "## Sanity check: the same exercise on BD TOPO", "",
              "Same reading, same rules (rank 5 or better, same built-up areas, same limits and cap, no prices) on the BD TOPO graph. "
              "BD TOPO is only a yardstick; it never steers the route above.", "",
              "| leg | length | duration |", "| --- | --- | --- |"] + \
             [f"| {l['name']} | {l['length_km']:.2f} km | {l['duration_min']:.1f} min |" for l in ref["legs"]] + \
             ["", f"Image network against BD TOPO: length {res['total']['length_km'] / ref['total']['length_km'] - 1:+.1%}, "
                  f"duration {res['total']['duration_min'] / ref['total']['duration_min'] - 1:+.1%}."]
    return "\n".join(L) + "\n"


def jsonable(res: dict) -> dict:
    out = json.loads(json.dumps({k: v for k, v in res.items() if k != "legs" and not k.startswith("_")}, default=str))
    out["legs"] = [{k: v for k, v in l.items() if k != "_line"} for l in res["legs"]]
    return out

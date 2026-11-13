"""S0 on the organisers' data: Scenario shapefiles (OSM Geofabrik, BD TOPO v2) and scanned maps.

Measured facts about the Scenario 1 delivery (see ``docs/real_data_validation.md``):

* both shapefiles come without ``.prj`` and ``.cpg``; coordinates are lon/lat
  degrees (bounds -2.848..-1.822 E, 47.476..48.666 N), so EPSG:4326 is assumed
  and checked against the bounds;
* the OSM file is UTF-8, the BD TOPO file is Windows-1252 (reading it as UTF-8
  fails);
* OSM is the Geofabrik ``gis_osm_roads/waterways/railways`` schema merged into
  one layer (``fclass``, ``oneway`` B/F/T, ``bridge``/``tunnel`` T/F, ``layer``);
  ways are not split at junctions;
* BD TOPO is the v2 ``ROUTE`` schema in upper case (``NATURE``, ``IMPORTANCE``,
  ``LARGEUR``, ``NB_VOIES``, ``POS_SOL``, ``FRANCHISST``, ``SENS``, ``Z_INI``,
  ``Z_FIN``); sections are noded at junctions;
* the two TIFFs are palette scans of the IGN 1:100 000 and 1:250 000 maps,
  EPSG:4326, same extent as the shapefiles.

Both loaders return the canonical graph of :mod:`coa_net.graph` and keep each
road's polyline (``coords``, lon/lat) so that curvature and GeoJSON stay exact.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Iterable

import networkx as nx
import numpy as np

from .graph import finalize_graph

log = logging.getLogger(__name__)

L93 = "EPSG:2154"

# OSM Geofabrik fclass values a wheeled convoy can use. Waterways, rail, paths,
# footways, cycleways, steps, bridleways and busways are dropped.
OSM_DRIVABLE = {
    "motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link", "secondary", "secondary_link",
    "tertiary", "tertiary_link", "unclassified", "residential", "living_street", "service",
    "track", "track_grade1", "track_grade2", "track_grade3", "track_grade4", "track_grade5", "unknown",
}
BDTOPO_EXCLUDED = {"sentier", "escalier", "piste cyclable"}


@dataclass
class LoadReport:
    """What the loader saw and did, for the validation report (measured, not assumed)."""

    source: str
    path: str
    features: int = 0
    kept: int = 0
    crs: str = ""
    crs_assumed: bool = False
    encoding: str = ""
    bounds: tuple = ()
    dropped: dict[str, int] = field(default_factory=dict)
    nodes: int = 0
    edges: int = 0
    km: float = 0.0
    largest_scc_share: float = 0.0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        rows = [
            ("source", self.source), ("file", self.path), ("features read", self.features), ("features kept", self.kept),
            ("CRS", self.crs + (" (assumed: no .prj, bounds are lon/lat)" if self.crs_assumed else "")),
            ("encoding", self.encoding), ("bounds", ", ".join(f"{b:.4f}" for b in self.bounds)),
            ("dropped", ", ".join(f"{k} {v}" for k, v in sorted(self.dropped.items(), key=lambda kv: -kv[1])) or "none"),
            ("graph nodes / directed edges", f"{self.nodes} / {self.edges}"), ("road length kept (km)", f"{self.km:.0f}"),
            ("largest strongly connected component", f"{self.largest_scc_share:.1%} of nodes"), ("load time (s)", f"{self.seconds:.1f}"),
        ]
        out = "| item | value |\n| --- | --- |\n" + "\n".join(f"| {k} | {v} |" for k, v in rows)
        if self.notes:
            out += "\n\n" + "\n".join("- " + n for n in self.notes)
        return out


def read_lines(path: str, encoding: str, columns: list[str] | None = None, bbox: tuple | None = None):
    """Read a line shapefile; if it has no CRS and its bounds are lon/lat, assume EPSG:4326."""
    import geopandas as gpd

    gdf = gpd.read_file(path, engine="pyogrio", encoding=encoding, columns=columns, bbox=bbox)
    assumed = False
    if gdf.crs is None:
        minx, miny, maxx, maxy = gdf.total_bounds
        if -180 <= minx <= maxx <= 180 and -90 <= miny <= maxy <= 90:
            gdf = gdf.set_crs("EPSG:4326")
            assumed = True
        else:
            raise ValueError(f"{path}: no CRS and bounds {gdf.total_bounds} are not lon/lat; give the CRS explicitly")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
    gdf = gdf.explode(index_parts=False).reset_index(drop=True)
    return gdf, assumed


def _text(v: Any) -> str:
    """A text field, '' when missing (None, NaN from the reader, BD TOPO's 'NC' / 'NR' codes)."""
    if v is None or (isinstance(v, float) and v != v):
        return ""
    t = str(v).strip()
    return "" if t in ("NC", "NR", "nan", "None") else t


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x != x else x


# ---------------------------------------------------------------------------
# OSM Geofabrik shapefile
# ---------------------------------------------------------------------------

def _osm_attrs(row: dict) -> tuple[dict, str]:
    fc = str(row.get("fclass") or "unclassified")
    hw = fc
    paved: bool | None = None
    if fc.startswith("track"):
        hw = "track"
        paved = True if fc == "track_grade1" else False if fc != "track" else None
    elif fc in ("motorway", "trunk", "primary", "secondary", "tertiary") or fc.endswith("_link"):
        paved = True
    ms = _num(row.get("maxspeed"))
    width = _num(row.get("width"))
    oneway = str(row.get("oneway") or "B")
    a = {
        "highway": hw,
        "fclass": fc,
        "maxspeed_kmh": ms if ms and ms > 0 else None,
        "width_m": width if width and width > 0 else None,
        "paved": paved,
        "bridge": str(row.get("bridge")) == "T",
        "tunnel": str(row.get("tunnel")) == "T",
        "built_up": fc in ("residential", "living_street"),
        "name": _text(row.get("ref")) or _text(row.get("name")),
        "source_id": f"osm:{row.get('osm_id')}",
    }
    return a, {"F": "forward", "T": "backward"}.get(oneway, "both")


def osm_segments(gdf) -> Iterable[tuple[list, dict, str]]:
    """Split Geofabrik ways at every vertex shared with another way (junctions), keep bridges/tunnels apart."""
    from collections import Counter

    rows = gdf.drop(columns="geometry").to_dict("records")
    coords = [np.round(np.asarray(g.coords)[:, :2], 7) for g in gdf.geometry.values]
    count: Counter = Counter()
    for c in coords:
        pts = {tuple(p) for p in c}
        count.update(pts)
    for c, row in zip(coords, rows):
        a, direction = _osm_attrs(row)
        cut = [0] + [i for i in range(1, len(c) - 1) if count[tuple(c[i])] > 1] + [len(c) - 1]
        for i, j in zip(cut[:-1], cut[1:]):
            if j > i:
                yield [tuple(p) for p in c[i:j + 1]], a, direction


# ---------------------------------------------------------------------------
# BD TOPO v2 shapefile (ROUTE layer, upper-case fields)
# ---------------------------------------------------------------------------

IMPORTANCE_TO_HIGHWAY = {"1": "trunk", "2": "primary", "3": "secondary", "4": "tertiary", "5": "unclassified"}


def _bd_attrs(row: dict) -> tuple[dict, str] | None:
    nature = str(row.get("NATURE") or "")
    nl = nature.lower()
    if nl in BDTOPO_EXCLUDED or str(row.get("ETAT") or "") == "En construction":
        return None
    imp = str(row.get("IMPORTANCE") or "NC").strip()
    if "autoroute" in nl:
        hw, paved = "motorway", True
    elif nl == "chemin" or "empierr" in nl:
        hw, paved = "track", False
    elif nl == "bretelle":
        hw, paved = IMPORTANCE_TO_HIGHWAY.get(imp, "unclassified") + "_link", True
    else:
        hw, paved = IMPORTANCE_TO_HIGHWAY.get(imp, "unclassified"), True
    if nl == "route à 2 chaussées" and hw in ("unclassified", "tertiary"):
        hw = "secondary"
    width = _num(row.get("LARGEUR"))
    lanes = int(row.get("NB_VOIES") or 0)
    pos = int(row.get("POS_SOL") or 0)
    fr = str(row.get("FRANCHISST") or "NC")
    name = _text(row.get("NUMERO")) or _text(row.get("NOM_VOIE_G"))
    zi, zf = _num(row.get("Z_INI")), _num(row.get("Z_FIN"))
    z_ok = _num(row.get("PREC_ALTI")) not in (None, 9999.0) and zi is not None and zf is not None and zi > -50 and zf > -50
    a = {
        "highway": hw,
        "nature": nature,
        "importance": imp,
        "width_m": width if width and 0 < width < 50 else None,  # 0 = unknown; 54 and 100 m are data errors
        # LARGEUR is rounded (90.5 % whole metres, 9.5 % half metres on Scenario 1): a 3 m road may be 3.5 m wide
        "width_prec_m": (0.5 if float(width).is_integer() else 0.25) if width and 0 < width < 50 else None,
        "lanes": lanes or None,
        "paved": paved,
        "bridge": pos > 0 or fr == "Pont",
        "tunnel": pos < 0 or fr == "Tunnel",
        "ford": fr == "Gué ou radier",
        "dual": nl in ("route à 2 chaussées", "quasi-autoroute"),
        "name": name,
        "commune": str(row.get("INSEECOM_G") or ""),
        "z": (zi, zf) if z_ok else None,
        "z_prec": _num(row.get("PREC_ALTI")) if z_ok else None,  # stated altitude precision (m) of each end
        "source_id": str(row.get("ID") or ""),
    }
    sens = str(row.get("SENS") or "NC")
    return a, {"Direct": "forward", "Inverse": "backward"}.get(sens, "both")


def bdtopo_segments(gdf) -> Iterable[tuple[list, dict, str]]:
    rows = gdf.drop(columns="geometry").to_dict("records")
    for g, row in zip(gdf.geometry.values, rows):
        r = _bd_attrs(row)
        if r is None:
            continue
        a, direction = r
        yield [tuple(p[:2]) for p in g.coords], a, direction


# ---------------------------------------------------------------------------
# graph assembly
# ---------------------------------------------------------------------------

def _geod_length(coords: list[tuple[float, float]]) -> float:
    lon = np.radians(np.asarray([c[0] for c in coords]))
    lat = np.radians(np.asarray([c[1] for c in coords]))
    if len(lon) < 2:
        return 0.0
    dlat, dlon = np.diff(lat), np.diff(lon)
    h = np.sin(dlat / 2) ** 2 + np.cos(lat[:-1]) * np.cos(lat[1:]) * np.sin(dlon / 2) ** 2
    return float((2 * 6_371_000 * np.arcsin(np.sqrt(h))).sum())


MAX_PLAUSIBLE_SLOPE_PCT = 35.0  # steeper "roads" in the data are altitude errors (seen: 130 %, 437 %)


def build_graph(segments: Iterable[tuple[list, dict, str]], name: str, simplify: bool = True,
                centrality_samples: int = 0) -> nx.DiGraph:
    """Canonical DiGraph from (lon/lat polyline, attributes, direction) segments.

    Parallel roads between the same two nodes keep the shortest. With
    ``simplify`` chains of degree-2 nodes whose roads share the same attributes
    are merged (polylines concatenated), which shrinks the graph several times
    without changing any shortest path.
    """
    G = nx.DiGraph(crs="EPSG:4326", name=name)
    ids: dict[tuple, int] = {}

    def node(p):
        k = (round(p[0], 7), round(p[1], 7))
        if k not in ids:
            ids[k] = len(ids)
            G.add_node(ids[k], x=k[0], y=k[1])
        return ids[k]

    for coords, a, direction in segments:
        if len(coords) < 2:
            continue
        u, v = node(coords[0]), node(coords[-1])
        if u == v:
            continue
        L = _geod_length(coords)
        if L <= 0:
            continue
        attrs = dict(a)
        attrs["length_m"] = L
        z = attrs.pop("z", None)
        prec = attrs.pop("z_prec", None) or 0.0
        if z is not None and L >= 20 and abs(z[1] - z[0]) / L * 100 <= MAX_PLAUSIBLE_SLOPE_PCT:
            dz = abs(z[1] - z[0])
            attrs["slope_pct"] = round(dz / L * 100, 1)
            # interval allowed by the stated precision of the two end altitudes: a requirement
            # blocks only on the low end, the band above it is priced as an unknown
            attrs["slope_lo_pct"] = round(max(dz - 2 * prec, 0.0) / L * 100, 1)
            attrs["slope_hi_pct"] = round((dz + 2 * prec) / L * 100, 1)
        elif z is not None and L >= 20:
            attrs["slope_suspect"] = True  # e.g. 130 % or 437 %: end altitudes of different levels; slope left unknown
        fw = dict(attrs, coords=tuple(coords))
        bw = dict(attrs, coords=tuple(reversed(coords)))
        if direction in ("forward", "both") and (not G.has_edge(u, v) or L < G[u][v]["length_m"]):
            G.add_edge(u, v, **fw)
        if direction in ("backward", "both") and (not G.has_edge(v, u) or L < G[v][u]["length_m"]):
            G.add_edge(v, u, **bw)
    if simplify:
        simplify_chains(G)
    return finalize_graph(G, centrality_samples=centrality_samples)


_SIG_KEYS = ("highway", "maxspeed_kmh", "width_m", "lanes", "paved", "bridge", "tunnel", "ford", "built_up", "name", "dual",
             "scan_symbol", "scan_gap", "scan_stream")  # scan_*: image network (scanroads.py); None on vector data


def _sig(d: dict) -> tuple:
    return tuple(d.get(k) for k in _SIG_KEYS)


def simplify_chains(G: nx.DiGraph) -> int:
    """Merge u->n->w chains (both directions alike) at nodes with exactly two neighbours and equal attributes."""
    removed = 0
    for n in list(G.nodes):
        preds, succs = set(G.predecessors(n)), set(G.successors(n))
        nb = preds | succs
        if len(nb) != 2:
            continue
        a, b = sorted(nb, key=str)
        pairs = [(a, b), (b, a)]
        live = [(x, y) for x, y in pairs if G.has_edge(x, n) and G.has_edge(n, y)]
        # every existing edge at n must be part of a through-chain, and both legs must look alike
        if len(live) == 0 or G.in_degree(n) != len(live) or G.out_degree(n) != len(live):
            continue
        ok = True
        for x, y in live:
            if _sig(G[x][n]) != _sig(G[n][y]) or G.has_edge(x, y) or x == y:
                ok = False
        if not ok:
            continue
        for x, y in live:
            d1, d2 = G[x][n], G[n][y]
            d = dict(d1)
            d["length_m"] = d1["length_m"] + d2["length_m"]
            d["coords"] = tuple(d1["coords"]) + tuple(d2["coords"][1:])
            for key in ("slope_pct", "slope_lo_pct", "slope_hi_pct"):
                vals = [e[key] for e in (d1, d2) if e.get(key) is not None]
                if vals:
                    d[key] = max(vals)
            G.add_edge(x, y, **d)
        G.remove_node(n)
        removed += 1
    return removed


def keep_largest_scc(G: nx.DiGraph) -> float:
    comps = list(nx.strongly_connected_components(G))
    if not comps:
        return 0.0
    big = max(comps, key=len)
    share = len(big) / G.number_of_nodes()
    G.remove_nodes_from([n for n in list(G) if n not in big])
    return share


def load_scenario_osm(path: str, bbox: tuple | None = None, simplify: bool = True) -> tuple[nx.DiGraph, LoadReport]:
    import time

    t = time.perf_counter()
    gdf, assumed = read_lines(path, "utf-8", ["fclass", "osm_id", "name", "ref", "oneway", "maxspeed", "layer", "bridge", "tunnel", "width"], bbox)
    rep = LoadReport("OSM (Geofabrik shapefile)", path, features=len(gdf), crs=str(gdf.crs), crs_assumed=assumed, encoding="UTF-8",
                     bounds=tuple(gdf.total_bounds))
    keep = gdf["fclass"].isin(OSM_DRIVABLE)
    rep.dropped = gdf.loc[~keep, "fclass"].value_counts().to_dict()
    gdf = gdf[keep]
    rep.kept = len(gdf)
    G = build_graph(osm_segments(gdf), name=f"osm:{path}", simplify=simplify)
    rep.largest_scc_share = keep_largest_scc(G)
    _finish(G, rep, t)
    return G, rep


def load_scenario_bdtopo(path: str, bbox: tuple | None = None, simplify: bool = True) -> tuple[nx.DiGraph, LoadReport]:
    import time

    t = time.perf_counter()
    cols = ["ID", "NATURE", "IMPORTANCE", "LARGEUR", "NB_VOIES", "POS_SOL", "FRANCHISST", "SENS", "NUMERO", "NOM_VOIE_G",
            "INSEECOM_G", "Z_INI", "Z_FIN", "PREC_ALTI", "ETAT"]
    gdf, assumed = read_lines(path, "cp1252", cols, bbox)
    rep = LoadReport("IGN BD TOPO v2 (ROUTE shapefile)", path, features=len(gdf), crs=str(gdf.crs), crs_assumed=assumed,
                     encoding="Windows-1252", bounds=tuple(gdf.total_bounds))
    nat = gdf["NATURE"].str.lower()
    drop = nat.isin(BDTOPO_EXCLUDED) | (gdf["ETAT"] == "En construction")
    rep.dropped = gdf.loc[drop, "NATURE"].value_counts().to_dict()
    if (gdf["ETAT"] == "En construction").any():
        rep.dropped["under construction"] = int((gdf["ETAT"] == "En construction").sum())
    rep.kept = int((~drop).sum())
    G = build_graph(bdtopo_segments(gdf), name=f"bdtopo:{path}", simplify=simplify)
    rep.largest_scc_share = keep_largest_scc(G)
    _finish(G, rep, t)
    return G, rep


def _finish(G: nx.DiGraph, rep: LoadReport, t0: float) -> None:
    import time

    from .graph import normalize_edge, ukey

    for _, _, d in G.edges(data=True):
        normalize_edge(d)
    rep.nodes, rep.edges = G.number_of_nodes(), G.number_of_edges()
    seen = set()
    km = 0.0
    for u, v, d in G.edges(data=True):
        k = ukey(u, v)
        if k not in seen:
            seen.add(k)
            km += d["length_m"] / 1000
    rep.km = km
    rep.seconds = time.perf_counter() - t0


def to_l93(lonlat: np.ndarray) -> np.ndarray:
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    x, y = tr.transform(lonlat[:, 0], lonlat[:, 1])
    return np.column_stack([x, y])


def radius_profile(coords_l93: np.ndarray) -> np.ndarray:
    """Circumradius (m) at each interior vertex of a polyline in metres; inf where straight."""
    p = np.asarray(coords_l93)
    if len(p) < 3:
        return np.full(len(p), math.inf)
    a, b, c = p[:-2], p[1:-1], p[2:]
    ab, bc, ca = np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)
    cross = np.abs((b - a)[:, 0] * (c - a)[:, 1] - (b - a)[:, 1] * (c - a)[:, 0])
    with np.errstate(divide="ignore", invalid="ignore"):
        r = ab * bc * ca / (2 * cross)
    r[~np.isfinite(r)] = math.inf
    return np.concatenate([[math.inf], r, [math.inf]])


def roadside_halts(G: nx.DiGraph, max_rank: int = 3, capacity: int = 30) -> list[str]:
    """Halt candidates when the data has no halt area at all.

    The seed doctrine plans a halt every 2 hours of driving at a halt area (R-23, R-24), and neither BD TOPO's
    ROUTE layer nor the OSM roads layer carries halt areas: without them every route over 2 hours violates
    R-23 and has no valid alternate. Junctions where at least three roads of rank ``max_rank`` or better meet
    (bridges and tunnels excluded) become roadside halt candidates with an assumed capacity; they are
    decision points of any corridor graph built with ``max_rank`` or more, so the graph does not change.
    PRO.CRITPOINT still keeps halts away from bridges and tunnels, and the halts a plan uses are to be
    confirmed by reconnaissance. Does nothing if the graph already has halt areas."""
    if any(d.get("halt_capacity") for _, d in G.nodes(data=True)):
        return []
    nb: dict[Any, set] = {}
    for u, v, d in G.edges(data=True):
        if d.get("road_rank", 5) <= max_rank and not (d.get("bridge") or d.get("tunnel")):
            nb.setdefault(u, set()).add(v)
            nb.setdefault(v, set()).add(u)
    cands = sorted((n for n, s in nb.items() if len(s) >= 3), key=str)
    for i, n in enumerate(cands, start=1):
        G.nodes[n].update(halt_capacity=capacity, halt_name=f"RH-{i}", halt_roadside=True)  # a label on every row: kept short
    return [f"no halt area in the data: {len(cands)} junctions of roads of rank {max_rank} or better are roadside halt "
            f"candidates, named RH-1 to RH-{len(cands)} (capacity {capacity} vehicles assumed); the halts a plan uses are to be "
            f"confirmed by reconnaissance"]


def systemic_unknowns(G: nx.DiGraph, requirements: list, threshold: float = 0.9) -> tuple[list, list[str]]:
    """Downgrade ``forbid`` to ``risk`` for attributes the map never carries.

    On the Scenario data no bridge has a load class (neither BD TOPO nor OSM
    carry MLC), so the seed rule "bridge class unknown -> forbidden" removes
    every bridge and splits the theatre along its rivers. When at least
    ``threshold`` of the relevant roads lack the attribute, the unknown is a
    property of the data source, not of the road: the instance is kept with
    policy ``risk`` (the road stays usable and priced; the verification counts
    such bridges on every designated route). Returns the adjusted requirement
    list and one note per change.
    """
    bridges = [d for _, _, d in G.edges(data=True) if d.get("bridge")]
    tunnels = [d for _, _, d in G.edges(data=True) if d.get("tunnel")]
    share = {
        "bridge_mlc": (sum(d.get("mlc") is None for d in bridges) / len(bridges)) if bridges else 0.0,
        "tunnel_height": (sum(d.get("max_height_m") is None for d in tunnels) / len(tunnels)) if tunnels else 0.0,
    }
    out, notes = [], []
    for r in requirements:
        key = None
        if r.template == "SEG.LOAD" and r.params.get("check") in ("bridge_mlc", "both") and r.on_unknown != "risk":
            key = "bridge_mlc"
        if key and share[key] >= threshold:
            out.append(r.model_copy(update={"on_unknown": "risk"}))
            notes.append(f"{r.id} {r.template}: {share[key]:.0%} of the {len(bridges)} bridge sections have no load class in the data; "
                         f"unknown policy '{r.on_unknown}' -> 'risk' (road usable and priced as a risk; the verification counts the unclassified bridges on every designated route or itinerary, to classify before the move)")
        else:
            out.append(r)
    return out, notes

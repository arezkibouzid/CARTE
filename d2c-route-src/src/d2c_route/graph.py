"""C0: annotated military road graph.

Every loader (synthetic, OSM, BD TOPO) produces a ``networkx.DiGraph`` with
node attributes ``x``, ``y`` (graph CRS; ``G.graph["crs"]`` is ``"EPSG:4326"``
for lon/lat or ``"local"`` for metres) and optional ``name``, and edge
attributes from :data:`d2c_route.expr.EDGE_ATTRS`. Missing values stay
``None`` (unknown), never optimistic defaults.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Any, Iterable

import networkx as nx

from .schemas import LocationRef

ROAD_RANK = {
    "motorway": 0,
    "trunk": 1,
    "primary": 2,
    "secondary": 3,
    "tertiary": 4,
    "unclassified": 5,
    "residential": 5,
    "service": 6,
    "track": 6,
}
DEFAULT_SPEED_KMH = {0: 110, 1: 90, 2: 80, 3: 70, 4: 60, 5: 40, 6: 25}

UNKNOWN_BY_DEFAULT = (
    "maxspeed_kmh",
    "width_m",
    "lanes",
    "paved",
    "max_weight_t",
    "mlc",
    "max_height_m",
    "slope_pct",
)
FALSE_BY_DEFAULT = ("bridge", "tunnel", "built_up", "forest", "closed")
ZERO_BY_DEFAULT = ("threat", "weather", "centrality")


def road_rank(highway: Any) -> int:
    if isinstance(highway, list):
        return min(road_rank(h) for h in highway)
    if not highway:
        return 5
    h = str(highway).replace("_link", "")
    return ROAD_RANK.get(h, 5)


def normalize_edge(d: dict) -> dict:
    d.setdefault("highway", "unclassified")
    d["road_rank"] = road_rank(d["highway"])
    for k in UNKNOWN_BY_DEFAULT:
        d.setdefault(k, None)
    for k in FALSE_BY_DEFAULT:
        d[k] = bool(d.get(k) or False)
    for k in ZERO_BY_DEFAULT:
        d[k] = float(d.get(k) or 0.0)
    d["length_m"] = float(d.get("length_m") or d.get("length") or 0.0)
    d.setdefault("name", "")
    return d


def speed_kmh(d: dict) -> float:
    """Legal/physical speed of the road (before convoy limits)."""
    if d.get("maxspeed_kmh"):
        return float(d["maxspeed_kmh"])
    return float(DEFAULT_SPEED_KMH[d.get("road_rank", 5)])


def finalize_graph(G: nx.DiGraph, centrality_samples: int = 200, seed: int = 0) -> nx.DiGraph:
    """Normalise attributes and compute an edge chokepoint indicator."""
    for _, _, d in G.edges(data=True):
        normalize_edge(d)
    if G.number_of_edges() and centrality_samples:
        k = min(centrality_samples, G.number_of_nodes())
        bc = nx.edge_betweenness_centrality(G, k=k, weight="length_m", seed=seed)
        top = max(bc.values()) or 1.0
        for (u, v), c in bc.items():
            G[u][v]["centrality"] = c / top
    G.graph.setdefault("crs", "local")
    return G


def is_geographic(G: nx.DiGraph) -> bool:
    return str(G.graph.get("crs", "local")).upper() in ("EPSG:4326", "WGS84")


def point_distance(G: nx.DiGraph, a: tuple[float, float], b: tuple[float, float]) -> float:
    """Distance in metres between two points in the graph CRS."""
    if is_geographic(G):
        lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
        h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
        return 2 * 6_371_000 * math.asin(math.sqrt(h))
    return math.hypot(a[0] - b[0], a[1] - b[1])


def node_xy(G: nx.DiGraph, n: Any) -> tuple[float, float]:
    return (G.nodes[n]["x"], G.nodes[n]["y"])


def edge_midpoint(G: nx.DiGraph, u: Any, v: Any) -> tuple[float, float]:
    (x1, y1), (x2, y2) = node_xy(G, u), node_xy(G, v)
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def nearest_node(G: nx.DiGraph, xy: tuple[float, float]) -> Any:
    return min(G.nodes, key=lambda n: point_distance(G, node_xy(G, n), xy))


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").split())


def gazetteer(G: nx.DiGraph) -> dict[str, Any]:
    """Folded place name -> node. Named nodes first, then road names (midpoint)."""
    out: dict[str, Any] = {}
    for n, d in G.nodes(data=True):
        if d.get("name"):
            out.setdefault(_fold(d["name"]), n)
    for u, v, d in G.edges(data=True):
        name = d.get("name")
        if isinstance(name, list):
            name = name[0] if name else ""
        if name:
            out.setdefault(_fold(str(name)), u)
    return out


def resolve(G: nx.DiGraph, loc: LocationRef) -> Any:
    if loc.node is not None:
        if loc.node in G:
            return loc.node
        # node ids from JSON may arrive as strings
        for cast in (int, str):
            try:
                if cast(loc.node) in G:
                    return cast(loc.node)
            except (TypeError, ValueError):
                pass
        raise KeyError(f"node {loc.node!r} not in graph")
    if loc.name is not None:
        gz = gazetteer(G)
        key = _fold(loc.name)
        if key in gz:
            return gz[key]
        partial = [k for k in gz if key in k or k in key]
        if len(partial) == 1:
            return gz[partial[0]]
        raise KeyError(f"place {loc.name!r} not found in gazetteer" + (f"; ambiguous: {partial}" if partial else ""))
    return nearest_node(G, loc.xy)


def location_xy(G: nx.DiGraph, loc: LocationRef) -> tuple[float, float]:
    if loc.xy is not None:
        return loc.xy
    return node_xy(G, resolve(G, loc))


def edges_within(G: nx.DiGraph, center: tuple[float, float], radius_m: float) -> list[tuple[Any, Any]]:
    return [(u, v) for u, v in G.edges if point_distance(G, edge_midpoint(G, u, v), center) <= radius_m]


def ukey(u: Any, v: Any) -> tuple[Any, Any]:
    """Direction-independent edge key (a convoy occupies the whole road)."""
    return (u, v) if str(u) <= str(v) else (v, u)


def path_edges(nodes: Iterable[Any]) -> list[tuple[Any, Any]]:
    nodes = list(nodes)
    return list(zip(nodes[:-1], nodes[1:]))


def set_edge_attr(G: nx.DiGraph, u: Any, v: Any, **attrs: Any) -> None:
    """Set attributes on both directions of a road, when both exist."""
    for a, b in ((u, v), (v, u)):
        if G.has_edge(a, b):
            G[a][b].update(attrs)

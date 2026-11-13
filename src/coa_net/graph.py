"""S0: annotated road graph.

Every loader (synthetic, OSM, BD TOPO) produces a ``networkx.DiGraph`` with
node attributes ``x``, ``y`` (graph CRS; ``G.graph["crs"]`` is ``"EPSG:4326"``
for lon/lat or ``"local"`` for metres) and optional ``name``,
``halt_capacity`` (halt-area candidate, vehicles) and ``fuel_point``.

Edge attributes follow one convention: a missing value stays ``None``
(unknown), never an optimistic default. COA-Net adds ``level_crossing``,
``ford``, ``ferry`` and ``defile`` (critical elements), ``lit``, and keeps the
initial ``threat0`` and ``weather0`` so that the belief model can recompute
the dynamic ``threat``, ``weather`` and ``closed`` values from reports.
"""

from __future__ import annotations

import functools
import math
import unicodedata
import weakref
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
    "lit",
)
FALSE_BY_DEFAULT = ("bridge", "tunnel", "built_up", "forest", "closed", "level_crossing", "ford", "ferry", "defile")
ZERO_BY_DEFAULT = ("threat", "weather", "centrality", "closed_p")
CRITICAL_KINDS = ("bridge", "tunnel", "level_crossing", "ford", "ferry", "defile")


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
    d.setdefault("threat0", d["threat"])
    d.setdefault("weather0", d["weather"])
    d["length_m"] = float(d.get("length_m") or d.get("length") or 0.0)
    d.setdefault("name", "")
    return d


def speed_kmh(d: dict) -> float:
    """Legal/physical speed of the road (before convoy limits)."""
    if d.get("maxspeed_kmh"):
        return float(d["maxspeed_kmh"])
    return float(DEFAULT_SPEED_KMH[d.get("road_rank", 5)])


def is_two_lane(d: dict, vehicle_width_m: float = 2.55, margin_m: float = 0.5) -> bool | None:
    """Can two columns meet on this road? None when unknown."""
    if d.get("lanes") is not None:
        return d["lanes"] >= 2
    if d.get("width_m") is not None:
        return d["width_m"] >= 2 * vehicle_width_m + margin_m
    return None


def finalize_graph(G: nx.DiGraph, centrality_samples: int = 200, seed: int = 0) -> nx.DiGraph:
    """Normalise attributes and compute an edge betweenness indicator (a chokepoint indicator)."""
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


def heading_deg(G: nx.DiGraph, a: Any, b: Any) -> float:
    (x1, y1), (x2, y2) = node_xy(G, a), node_xy(G, b)
    if is_geographic(G):
        x1 *= math.cos(math.radians(y1))
        x2 *= math.cos(math.radians(y2))
    return math.degrees(math.atan2(y2 - y1, x2 - x1))


def turn_angle(G: nx.DiGraph, a: Any, b: Any, c: Any) -> float:
    """Change of heading at ``b`` when driving a -> b -> c, in degrees (0 = straight on, 180 = U-turn)."""
    d = abs(heading_deg(G, b, c) - heading_deg(G, a, b)) % 360
    return 360 - d if d > 180 else d


@functools.lru_cache(maxsize=65536)
def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace("-", " ").split())


# road-name part of the gazetteer per graph, rebuilt when the graph's size changes
# (on a national road graph it costs seconds and every order and what-if world asks for it)
_ROAD_NAMES: "weakref.WeakKeyDictionary[nx.DiGraph, tuple[tuple[int, int], dict[str, Any]]]" = weakref.WeakKeyDictionary()


def gazetteer(G: nx.DiGraph) -> dict[str, Any]:
    """Folded place name -> node. Named nodes first, then road names (start node)."""
    out: dict[str, Any] = {}
    for n, d in G.nodes(data=True):
        if d.get("name"):
            out.setdefault(_fold(d["name"]), n)
    sig = (G.number_of_nodes(), G.number_of_edges())
    cached = _ROAD_NAMES.get(G)
    if cached is None or cached[0] != sig:
        roads: dict[str, Any] = {}
        for u, v, d in G.edges(data=True):
            name = d.get("name")
            if isinstance(name, list):
                name = name[0] if name else ""
            if name:
                roads.setdefault(_fold(str(name)), u)
        cached = _ROAD_NAMES[G] = (sig, roads)
    for k, u in cached[1].items():
        out.setdefault(k, u)
    return out


def resolve(G: nx.DiGraph, loc: LocationRef) -> Any:
    if loc.node is not None:
        if loc.node in G:
            return loc.node
        for cast in (int, str):  # node ids from JSON may arrive as strings
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
    """Direction-independent edge key (both directions of one road)."""
    return (u, v) if str(u) <= str(v) else (v, u)


def path_edges(nodes: Iterable[Any]) -> list[tuple[Any, Any]]:
    nodes = list(nodes)
    return list(zip(nodes[:-1], nodes[1:]))


def path_length_m(G: nx.DiGraph, nodes: list[Any]) -> float:
    return sum(G[u][v]["length_m"] for u, v in path_edges(nodes))


def set_edge_attr(G: nx.DiGraph, u: Any, v: Any, **attrs: Any) -> None:
    """Set attributes on both directions of a road, when both exist."""
    for a, b in ((u, v), (v, u)):
        if G.has_edge(a, b):
            G[a][b].update(attrs)


def place_label(G: nx.DiGraph, n: Any) -> str:
    return G.nodes[n].get("name") or f"node {n}"


__all__ = [
    "CRITICAL_KINDS",
    "DEFAULT_SPEED_KMH",
    "ROAD_RANK",
    "edge_midpoint",
    "edges_within",
    "finalize_graph",
    "gazetteer",
    "heading_deg",
    "is_geographic",
    "is_two_lane",
    "location_xy",
    "nearest_node",
    "node_xy",
    "normalize_edge",
    "path_edges",
    "path_length_m",
    "place_label",
    "point_distance",
    "resolve",
    "road_rank",
    "set_edge_attr",
    "speed_kmh",
    "turn_angle",
    "ukey",
]

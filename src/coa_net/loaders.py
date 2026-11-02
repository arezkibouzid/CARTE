"""S0 loaders: OpenStreetMap (stand-in) and IGN BD TOPO (target data).

Both produce the canonical graph described in :mod:`coa_net.graph`.
Attributes OSM does not carry (bridge MLC, most widths) stay unknown. COA-Net
adds the critical-element flags ``level_crossing`` (OSM node
``railway=level_crossing``, set on the roads that meet there) and ``ford``,
and the ``lit`` attribute (night movement). Field names for BD TOPO follow the
v3 ``TRONCON_DE_ROUTE`` layer; check them on the organisers' extract and
adjust :data:`BDTOPO_FIELDS` if needed.

Optional dependencies: ``pip install -e .[osm]`` (osmnx, geopandas, shapely);
``rasterio`` for slopes from a DEM (e.g. IGN RGE ALTI).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import networkx as nx

from .graph import finalize_graph

log = logging.getLogger(__name__)

PAVED = {"asphalt", "concrete", "paved", "paving_stones", "sett", "concrete:plates", "concrete:lanes", "chipseal", "metal"}
UNPAVED = {"unpaved", "gravel", "dirt", "ground", "grass", "sand", "compacted", "fine_gravel", "earth", "mud", "pebblestone", "rock"}
OSM_TAGS = ["bridge", "tunnel", "maxweight", "maxheight", "maxwidth", "width", "surface", "tracktype", "lanes", "maxspeed", "layer", "name", "ref",
            "highway", "ford", "lit"]
OSM_NODE_TAGS = ["highway", "junction", "railway", "ref"]
SPEED_ZONES = {"fr:urban": 50, "fr:rural": 80, "fr:motorway": 130, "fr:zone30": 30, "fr:walk": 6}


def _first(v: Any) -> Any:
    return v[0] if isinstance(v, list) and v else v


def parse_number(v: Any, unit: str = "") -> float | None:
    """'7.5', '7.5 t', '7500 kg', '3.8 m', "12'6\"" -> float in t or m; None if unparsable."""
    v = _first(v)
    if v is None or (isinstance(v, float) and v != v):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().replace(",", ".")
    m = re.match(r"^(\d+)'\s*(\d+)?\"?$", s)  # feet and inches
    if m:
        return round(int(m.group(1)) * 0.3048 + int(m.group(2) or 0) * 0.0254, 2)
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-z]*)", s)
    if not m:
        return None
    x, u = float(m.group(1)), m.group(2)
    if u == "kg":
        return x / 1000
    if u in ("st",):  # short tons
        return x * 0.907
    if u == "mph":
        return x * 1.609
    return x


def _speed(v: Any) -> float | None:
    v = _first(v)
    if v is None:
        return None
    s = str(v).lower()
    if s in SPEED_ZONES:
        return float(SPEED_ZONES[s])
    if s == "none":
        return None
    return parse_number(s)


def _flag(v: Any) -> bool:
    v = _first(v)
    return v is not None and str(v).lower() not in ("no", "false", "0", "nan")


def _tri(v: Any) -> bool | None:
    """yes/no tag -> True/False, missing -> None (unknown)."""
    v = _first(v)
    if v is None or (isinstance(v, float) and v != v):
        return None
    return str(v).lower() not in ("no", "false", "0", "disused")


def osm_edge_attrs(d: dict) -> dict:
    hw = _first(d.get("highway")) or "unclassified"
    surface = str(_first(d.get("surface")) or "").lower()
    paved: bool | None = True if surface in PAVED else False if surface in UNPAVED else None
    if paved is None and hw == "track":
        tt = str(_first(d.get("tracktype")) or "")
        paved = True if tt == "grade1" else False if tt.startswith("grade") else None
    if paved is None and hw in ("motorway", "trunk", "primary", "secondary", "tertiary"):
        paved = True  # classified roads in France are paved
    width = parse_number(d.get("width")) or parse_number(d.get("maxwidth"))
    weight = parse_number(d.get("maxweight"))
    bridge = _flag(d.get("bridge"))
    lanes = parse_number(d.get("lanes"))
    return {
        "length_m": float(d.get("length", 0.0)),
        "highway": hw,
        "maxspeed_kmh": _speed(d.get("maxspeed")),
        "width_m": width,
        "lanes": int(lanes) if lanes else None,
        "paved": paved,
        "max_weight_t": weight,
        # OSM has no MLC; a posted weight limit on a bridge is the best available proxy
        "mlc": int(weight) if bridge and weight else None,
        "bridge": bridge,
        "tunnel": _flag(d.get("tunnel")),
        "max_height_m": parse_number(d.get("maxheight")),
        "built_up": hw in ("residential", "living_street"),
        "ford": _flag(d.get("ford")),
        "lit": _tri(d.get("lit")),
        "name": str(_first(d.get("name")) or _first(d.get("ref")) or ""),
    }


def osm_to_graph(Gm: nx.MultiDiGraph) -> nx.DiGraph:
    """Collapse an OSMnx MultiDiGraph into the canonical DiGraph (shortest parallel edge)."""
    G = nx.DiGraph(crs="EPSG:4326", name=Gm.graph.get("name", "osm"))
    crossings = set()
    for n, d in Gm.nodes(data=True):
        G.add_node(n, x=float(d["x"]), y=float(d["y"]))
        if str(_first(d.get("railway")) or "").lower() == "level_crossing":
            crossings.add(n)
    for u, v, d in Gm.edges(data=True):
        a = osm_edge_attrs(d)
        if u in crossings or v in crossings:
            a["level_crossing"] = True
        if not G.has_edge(u, v) or a["length_m"] < G[u][v]["length_m"]:
            G.add_edge(u, v, **a)
    return G


def load_osm(
    place: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    point: tuple[float, float] | None = None,
    dist_m: float = 15000,
    graphml: str | None = None,
    network_type: str = "drive",
    landuse: bool = True,
    dem: str | None = None,
    places: dict[str, tuple[float, float]] | None = None,
) -> nx.DiGraph:
    """Download (or load from GraphML) an OSM road graph and annotate it.

    ``bbox`` is (west, south, east, north) in degrees; ``point`` is (lat, lon).
    ``places`` maps names to (lon, lat) to seed the gazetteer.
    """
    import osmnx as ox

    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) | set(OSM_TAGS))
    ox.settings.useful_tags_node = sorted(set(ox.settings.useful_tags_node) | set(OSM_NODE_TAGS))
    if graphml:
        Gm = ox.load_graphml(graphml)
    elif place:
        Gm = ox.graph_from_place(place, network_type=network_type, simplify=True)
    elif bbox:
        Gm = ox.graph_from_bbox(bbox, network_type=network_type, simplify=True)
    elif point:
        Gm = ox.graph_from_point(point, dist=dist_m, network_type=network_type, simplify=True)
    else:
        raise ValueError("give place, bbox, point or graphml")
    Gm = ox.truncate.largest_component(Gm, strongly=True)
    G = osm_to_graph(Gm)
    if landuse:
        try:
            annotate_landuse_osm(G)
        except Exception as exc:  # noqa: BLE001 - optional enrichment
            log.warning("land-use annotation skipped: %s", exc)
    if dem:
        add_slopes_from_dem(G, dem)
    if places:
        add_places(G, places)
    return finalize_graph(G)


def save_graphml_osm(place: str, path: str, network_type: str = "drive") -> None:
    """Cache an OSM download for offline use (e.g. at the hackathon)."""
    import osmnx as ox

    ox.settings.useful_tags_way = sorted(set(ox.settings.useful_tags_way) | set(OSM_TAGS))
    ox.settings.useful_tags_node = sorted(set(ox.settings.useful_tags_node) | set(OSM_NODE_TAGS))
    ox.save_graphml(ox.graph_from_place(place, network_type=network_type), path)


def _edge_points(G: nx.DiGraph):
    import geopandas as gpd
    from shapely.geometry import Point

    keys = list(G.edges)
    pts = [Point((G.nodes[u]["x"] + G.nodes[v]["x"]) / 2, (G.nodes[u]["y"] + G.nodes[v]["y"]) / 2) for u, v in keys]
    return keys, gpd.GeoDataFrame({"k": range(len(keys))}, geometry=pts, crs=G.graph["crs"])


def annotate_landuse_osm(G: nx.DiGraph) -> None:
    """Flag edges inside built-up land use or forest polygons (OSM)."""
    import geopandas as gpd
    import osmnx as ox
    from shapely.geometry import MultiPoint

    hull = MultiPoint([(d["x"], d["y"]) for _, d in G.nodes(data=True)]).convex_hull
    feats = ox.features_from_polygon(
        hull,
        tags={"landuse": ["residential", "commercial", "industrial", "retail", "forest"], "natural": ["wood"]},
    )
    feats = feats[feats.geometry.type.isin(["Polygon", "MultiPolygon"])].copy()
    lu = feats["landuse"] if "landuse" in feats else None
    nat = feats["natural"] if "natural" in feats else None
    feats["kind"] = "built_up"
    if lu is not None:
        feats.loc[lu == "forest", "kind"] = "forest"
    if nat is not None:
        feats.loc[nat == "wood", "kind"] = "forest"
    keys, pts = _edge_points(G)
    joined = gpd.sjoin(pts, feats[["kind", "geometry"]], predicate="within", how="inner")
    for i, kind in zip(joined["k"], joined["kind"]):
        u, v = keys[i]
        G[u][v][kind] = True


def add_slopes_from_dem(G: nx.DiGraph, dem_path: str) -> None:
    """Edge slope (%) from a DEM raster sampled at nodes (needs rasterio)."""
    import rasterio
    from rasterio.warp import transform

    with rasterio.open(dem_path) as src:
        nodes = list(G.nodes)
        xs = [G.nodes[n]["x"] for n in nodes]
        ys = [G.nodes[n]["y"] for n in nodes]
        if str(src.crs).upper() != str(G.graph["crs"]).upper():
            xs, ys = transform(G.graph["crs"], src.crs, xs, ys)
        z = {n: float(v[0]) for n, v in zip(nodes, src.sample(zip(xs, ys)))}
        nodata = src.nodata
    for u, v, d in G.edges(data=True):
        if nodata is not None and (z[u] == nodata or z[v] == nodata) or d["length_m"] <= 0:
            continue
        d["slope_pct"] = round(abs(z[v] - z[u]) / d["length_m"] * 100, 1)


def add_places(G: nx.DiGraph, places: dict[str, tuple[float, float]], snap_max_rank: int | None = None) -> dict[str, dict]:
    """Name the node nearest to each place; returns where each place was put.

    With ``snap_max_rank`` the node must be on a road of at most that rank: a
    column starts and ends on a road it can use, not in the side street or on
    the track nearest to the label of a town on a map (on the Scenario 1 map
    the nearest node to "Questembert" is on an unpaved track)."""
    import numpy as np

    from .graph import is_geographic

    if snap_max_rank is None:
        cand = list(G.nodes)
    else:
        cand = sorted({n for u, v, d in G.edges(data=True) if d.get("road_rank", 5) <= snap_max_rank for n in (u, v)}, key=str) or list(G.nodes)
    xs = np.array([G.nodes[n]["x"] for n in cand], float)
    ys = np.array([G.nodes[n]["y"] for n in cand], float)
    out: dict[str, dict] = {}
    for name, (x, y) in places.items():
        if is_geographic(G):
            d = np.hypot((xs - x) * 111_320 * np.cos(np.radians(y)), (ys - y) * 110_540)
        else:
            d = np.hypot(xs - x, ys - y)
        i = int(np.argmin(d))
        n = cand[i]
        G.nodes[n]["name"] = name
        roads = sorted({str(dd.get("name")) for _, _, dd in G.edges(n, data=True) if dd.get("name")})
        out[name] = {"node": n, "distance_m": round(float(d[i])), "roads": roads}
    return out


def add_halt_areas(G: nx.DiGraph, areas: dict[str, tuple[tuple[float, float], int]]) -> None:
    """Halt-area candidates from a survey: name -> ((x, y), capacity in vehicles), snapped to the nearest node."""
    from .graph import nearest_node

    for name, (xy, cap) in areas.items():
        n = nearest_node(G, xy)
        G.nodes[n]["halt_capacity"] = int(cap)
        G.nodes[n]["halt_name"] = name


# ---------------------------------------------------------------------------
# IGN BD TOPO
# ---------------------------------------------------------------------------

BDTOPO_FIELDS = {
    "nature": "nature",
    "importance": "importance",
    "width": "largeur_de_chaussee",
    "lanes": "nombre_de_voies",
    "level": "position_par_rapport_au_sol",
    "max_weight": "restriction_de_poids_total",
    "max_height": "restriction_de_hauteur",
    "direction": "sens_de_circulation",
    "speed": "vitesse_moyenne_vl",
    "number": "cpx_numero",
    "name": "nom_1_gauche",
    "access": "acces_vehicule_leger",
    "urban": "urbain",
}
BDTOPO_EXCLUDED = {"sentier", "escalier", "piste cyclable", "bac ou liaison maritime", "bac", "chemin piéton"}
IMPORTANCE_TO_HIGHWAY = {"1": "trunk", "2": "primary", "3": "secondary", "4": "tertiary", "5": "unclassified", "6": "track"}


def _bd_highway(nature: str, importance: Any) -> tuple[str, bool | None]:
    n = (nature or "").lower()
    if "autoroute" in n:
        return "motorway", True
    if "empierr" in n or n == "chemin":
        return "track", False
    hw = IMPORTANCE_TO_HIGHWAY.get(str(importance).strip(), "unclassified")
    return hw, None if hw == "track" else True


def load_bdtopo(path: str, layer: str = "troncon_de_route", bbox: tuple[float, float, float, float] | None = None, fields: dict | None = None) -> nx.DiGraph:
    """Build the canonical graph from a BD TOPO GeoPackage/Shapefile road layer.

    ``bbox`` is in the file CRS (Lambert-93, EPSG:2154, for BD TOPO).
    """
    import geopandas as gpd

    f = {**BDTOPO_FIELDS, **(fields or {})}
    gdf = gpd.read_file(path, layer=layer, bbox=bbox)
    gdf.columns = [c.lower() if c != "geometry" else c for c in gdf.columns]
    gdf = gdf[gdf.geometry.notna()].explode(index_parts=False)
    lengths = gdf.geometry.length if gdf.crs and gdf.crs.is_projected else None
    wgs = gdf.to_crs("EPSG:4326")
    G = nx.DiGraph(crs="EPSG:4326", name=f"bdtopo:{path}")

    def get(row, key):
        col = f.get(key)
        v = row.get(col) if col else None
        return None if v is None or (isinstance(v, float) and v != v) else v

    node_ids: dict[tuple[float, float], int] = {}

    def node(xy):
        k = (round(xy[0], 7), round(xy[1], 7))
        if k not in node_ids:
            node_ids[k] = len(node_ids)
            G.add_node(node_ids[k], x=k[0], y=k[1])
        return node_ids[k]

    for idx, (row, geom) in enumerate(zip(wgs.drop(columns="geometry").to_dict("records"), wgs.geometry)):
        nature = str(get(row, "nature") or "")
        if nature.lower() in BDTOPO_EXCLUDED or str(get(row, "access") or "").lower().startswith("physiquement impossible"):
            continue
        coords = list(geom.coords)
        u, v = node(coords[0]), node(coords[-1])
        if u == v:
            continue
        hw, paved = _bd_highway(nature, get(row, "importance"))
        level = parse_number(get(row, "level")) or 0
        urban = get(row, "urban")
        name = get(row, "number") or get(row, "name") or ""
        a = {
            "length_m": float(lengths.iloc[idx]) if lengths is not None else 0.0,
            "highway": hw,
            "maxspeed_kmh": parse_number(get(row, "speed")),
            "width_m": parse_number(get(row, "width")) or None,
            "lanes": int(parse_number(get(row, "lanes")) or 0) or None,
            "paved": paved,
            "max_weight_t": parse_number(get(row, "max_weight")),
            "mlc": None,  # not in BD TOPO: to be supplied from engineer route reconnaissance
            "bridge": level > 0,
            "tunnel": level < 0,
            "max_height_m": parse_number(get(row, "max_height")),
            "built_up": str(urban).lower() in ("true", "1", "oui", "vrai") if urban is not None else False,
            "ford": "gué" in nature.lower() or "gue" == nature.lower(),
            "name": str(name),
        }
        if a["length_m"] == 0.0:
            from .graph import point_distance

            a["length_m"] = sum(point_distance(G, coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        direction = str(get(row, "direction") or "double sens").lower()
        if "inverse" in direction:
            G.add_edge(v, u, **a)
        elif "direct" in direction:
            G.add_edge(u, v, **a)
        else:  # double sens / sans objet
            G.add_edge(u, v, **a)
            G.add_edge(v, u, **dict(a))
    largest = max(nx.strongly_connected_components(G), key=len)
    G.remove_nodes_from([n for n in list(G) if n not in largest])
    return finalize_graph(G)

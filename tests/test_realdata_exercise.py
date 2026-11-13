"""Real-data adapters and exercise mode, on small synthetic inputs (the Scenario files are not in the repo)."""

import math

import networkx as nx
import numpy as np
import pytest

geopandas = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")
pytest.importorskip("scipy")

from coa_net.exercise import RoadArrays, chord_radius, parse_exercise, resample, solve_distinct, turns_on  # noqa: E402
from coa_net.realdata import _bd_attrs, build_graph, osm_segments  # noqa: E402
from coa_net.requirements import TEMPLATES  # noqa: E402
from coa_net.scanmap import Label, towns_from_labels  # noqa: E402
from coa_net.scenario import EXERCISE_1  # noqa: E402


def test_parse_exercise_1():
    spec, reqs = parse_exercise(EXERCISE_1)
    assert spec.k == 3 and spec.min_sep_m == 5000 and spec.no_intersect
    assert spec.min_town_pop == 10000 and spec.vehicle == "pfm_carrier"
    assert (spec.origin_zone, spec.destination_zone) == ("South", "North")
    assert {r.template for r in reqs} == {"NET.DISTINCT", "SEG.TOWN"}
    assert all(r.template in TEMPLATES for r in reqs)
    assert len(spec.outputs) == 4


def test_bdtopo_v2_attributes():
    row = {"NATURE": "Route à 1 chaussée", "IMPORTANCE": "3", "LARGEUR": 0.0, "NB_VOIES": 2, "POS_SOL": 1, "FRANCHISST": "Pont",
           "SENS": "Direct", "NUMERO": "D766", "Z_INI": 50.0, "Z_FIN": 54.0, "PREC_ALTI": 1.0, "ETAT": "NR", "ID": "X"}
    a, direction = _bd_attrs(row)
    assert a["highway"] == "secondary" and a["bridge"] and a["width_m"] is None and a["lanes"] == 2
    assert direction == "forward" and a["name"] == "D766" and a["z"] == (50.0, 54.0)
    assert _bd_attrs({**row, "NATURE": "Sentier"}) is None
    a, _ = _bd_attrs({**row, "NATURE": "Chemin", "POS_SOL": 0, "FRANCHISST": "NC", "PREC_ALTI": 9999.0})
    assert a["highway"] == "track" and a["paved"] is False and not a["bridge"] and a["z"] is None


def test_osm_ways_split_at_shared_vertices():
    from shapely.geometry import LineString

    gdf = geopandas.GeoDataFrame(
        {"fclass": ["primary", "residential"], "oneway": ["B", "F"], "osm_id": [1, 2], "bridge": ["F", "F"], "tunnel": ["F", "F"]},
        geometry=[LineString([(0, 0), (0.001, 0), (0.002, 0)]), LineString([(0.001, -0.001), (0.001, 0), (0.001, 0.001)])], crs="EPSG:4326")
    segs = list(osm_segments(gdf))
    assert len(segs) == 4  # both ways cut at their shared vertex
    G = build_graph(segs, "t", simplify=False)
    assert G.number_of_nodes() == 5
    assert G.has_edge(G_node(G, 0.001, -0.001), G_node(G, 0.001, 0)) and not G.has_edge(G_node(G, 0.001, 0), G_node(G, 0.001, -0.001))


def G_node(G, x, y):
    return next(n for n, d in G.nodes(data=True) if abs(d["x"] - x) < 1e-9 and abs(d["y"] - y) < 1e-9)


def test_chain_simplification_keeps_lengths():
    segs = [([(0, 0), (0.001, 0)], {"highway": "primary"}, "both"), ([(0.001, 0), (0.002, 0)], {"highway": "primary"}, "both")]
    G = build_graph(segs, "t", simplify=True)
    assert G.number_of_edges() == 2  # one road, both directions
    u, v = next(iter(G.edges))
    assert G[u][v]["length_m"] == pytest.approx(2 * 111.19 * math.cos(0), rel=1e-2)
    assert len(G[u][v]["coords"]) == 3


def test_turn_radius_on_a_known_arc():
    from shapely.geometry import LineString

    for r, severity in ((20.0, "difficult"), (50.0, None)):
        t = np.linspace(math.pi / 2, math.pi, 80)
        line = LineString([(200, 0)] + [(r * math.cos(a), -r + r * math.sin(a)) for a in t] + [(-r, -200)])
        assert np.nanmin(chord_radius(resample(line, 5.0), 2)) == pytest.approx(r, rel=0.05)
        turns = turns_on(line, 30, 15)
        assert [x["severity"] for x in turns] == ([severity] if severity else [])
    corner = LineString([(100, 0), (0, 0), (0, -100)])  # a right-angle junction turn reads as critical
    assert turns_on(corner, 30, 15)[0]["severity"] == "critical"


def test_towns_from_labels_pairs_name_and_figure():
    L = [Label("VANNES", 0.8, -2.78, 47.64, 100, 100, 40, 120), Label("53,0", 0.6, -2.78, 47.63, 110, 135, 22, 40),
         Label("ST-AVE", 0.8, -2.70, 47.68, 500, 200, 36, 100), Label("10,8", 0.8, -2.70, 47.69, 502, 171, 25, 40),
         Label("1,5", 0.8, -2.5, 47.7, 900, 900, 24, 30)]
    towns = {t.name: t.population for t in towns_from_labels(L)}
    assert towns == {"VANNES": 53000, "ST-AVE": 10800}


def _grid_graph(nx_=9, ny_=12, step=0.01):
    """Parallel north-south roads every ``step`` degrees of longitude, linked by east-west roads."""
    segs = []
    for i in range(nx_):
        for j in range(ny_ - 1):
            x, y0, y1 = -2.5 + i * step, 47.5 + j * step, 47.5 + (j + 1) * step
            segs.append(([(x, y0), (x, y1)], {"highway": "secondary"}, "both"))
    for j in range(ny_):
        for i in range(nx_ - 1):
            x0, x1, y = -2.5 + i * step, -2.5 + (i + 1) * step, 47.5 + j * step
            segs.append(([(x0, y), (x1, y)], {"highway": "tertiary"}, "both"))
    return build_graph(segs, "grid", simplify=False)


def test_distinct_itineraries_are_separated():
    from types import SimpleNamespace

    G = _grid_graph()
    evals = {e: SimpleNamespace(ok=True, time_min=G[e[0]][e[1]]["length_m"] / 1000, unknown=[]) for e in G.edges}
    R = RoadArrays.build(G, evals)
    ys = np.array([G.nodes[n]["y"] for n in R.nodes])
    src, dst = np.flatnonzero(ys < 47.5001), np.flatnonzero(ys > 47.5 + 0.11 - 1e-4)
    res = solve_distinct(R, src, dst, k=3, sep_m=1400.0, n_seeds=6)
    assert len(res.itineraries) == 3
    for i in range(3):
        for j in range(i + 1, 3):
            assert shapely.distance(res.itineraries[i].line, res.itineraries[j].line) >= 1400.0
    # 4 itineraries 1.4 km apart do not fit in 9 roads 0.75 km apart over 6 km: reported as infeasible, not faked
    res4 = solve_distinct(R, src, dst, k=4, sep_m=2300.0, n_seeds=6)
    assert res4.itineraries == [] and res4.feasible_sets == 0


def test_turn_aware_router_avoids_staircases():
    from types import SimpleNamespace

    from coa_net.exercise import Router, TurnRouter

    G = _grid_graph()
    evals = {e: SimpleNamespace(ok=True, time_min=G[e[0]][e[1]]["length_m"] / 1000, unknown=[]) for e in G.edges}
    R = RoadArrays.build(G, evals)
    xs = np.array([G.nodes[n]["x"] for n in R.nodes])
    ys = np.array([G.nodes[n]["y"] for n in R.nodes])
    src = np.flatnonzero((xs < -2.4999) & (ys < 47.5001))
    dst = np.flatnonzero((xs > -2.5 + 0.08 - 1e-4) & (ys > 47.5 + 0.11 - 1e-4))
    tr = TurnRouter(R, src, dst)
    e_turn = tr.shortest(R.ok)
    e_plain = Router(R, src, dst).shortest(R.ok)
    sharp_turn = int((tr.turns_of(e_turn) > 60).sum())
    sharp_plain = int((tr.turns_of(e_plain) > 60).sum())
    assert sharp_turn == 1  # one L-shaped turn
    assert sharp_plain >= sharp_turn
    assert R.time_min[e_turn].sum() == pytest.approx(R.time_min[e_plain].sum(), rel=1e-6)  # same length on a grid
    res = solve_distinct(R, src, dst, k=1, sep_m=1000.0, n_seeds=3, router=tr)
    assert len(res.itineraries) == 1


def test_slope_blocks_only_beyond_the_altitude_precision():
    from coa_net.corridors import CorridorGraph
    from coa_net.costing import GroupCoster
    from coa_net.requirements import ActiveSet, load_requirements
    from coa_net.vehicles import Profile

    dx = 30.0 / (111_320 * math.cos(math.radians(47.5)))  # 30 m sections at 47.5 N
    base = {"highway": "tertiary", "z_prec": 1.0}
    segs = [([(-2.5, 47.5), (-2.5 + dx, 47.5)], {**base, "z": (50.0, 56.0)}, "forward"),  # 20 %, but 13.3 % within precision
            ([(-2.5 + dx, 47.5), (-2.5 + 2 * dx, 47.5)], {**base, "z": (56.0, 63.0)}, "forward"),  # 23 %, at least 16.7 %
            ([(-2.5 + 2 * dx, 47.5), (-2.5 + 3 * dx, 47.5)], {**base, "z": (63.0, 63.5)}, "forward")]  # flat
    G = build_graph(segs, "t", simplify=False)
    e = sorted(G.edges, key=lambda uv: G.nodes[uv[0]]["x"])
    assert [G.edges[x]["slope_pct"] for x in e] == pytest.approx([20.0, 23.3, 1.7], abs=0.2)
    assert G.edges[e[0]]["slope_lo_pct"] == pytest.approx(13.3, abs=0.2) and G.edges[e[0]]["slope_hi_pct"] == pytest.approx(26.7, abs=0.2)
    grade = [r for r in load_requirements() if r.template == "SEG.GRADE" and r.tier == "T0"]
    prof = Profile(ids=["P"], vehicle_classes={"heavy"}, cargo=set(), weight_t=32, mlc=32, width_m=3.0, height_m=3.9, length_m=12,
                   max_speed_kmh=70, max_slope_pct=15, tracked=False, vehicle_lengths=[12.0] * 6)
    c = GroupCoster(CorridorGraph(G), prof, ActiveSet(grade))
    ev = [c.evals[x] for x in e]
    assert ev[0].ok and ev[0].unknown and "precision" in ev[0].unknown[0][1]  # priced as a risk, not excluded
    assert not ev[1].ok and "slope" in ev[1].blocked[0][2]
    assert ev[2].ok and not ev[2].unknown


def test_exercise_pdf_gives_the_largest_image(tmp_path):
    pytest.importorskip("pypdf")
    from PIL import Image

    from coa_net.scenario import exercise_from_pdf

    big, small = Image.new("RGB", (300, 400), (0, 0, 255)), Image.new("RGB", (50, 40), (255, 0, 0))
    big.save(tmp_path / "ex.pdf", save_all=True, append_images=[small])
    text, sketch = exercise_from_pdf(tmp_path / "ex.pdf", tmp_path / "input")
    assert Image.open(sketch).size == (300, 400) and (tmp_path / "input" / "exercise.txt").exists()


def test_scan_check_finds_routes_on_road_ink(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin
    from shapely.geometry import LineString

    from coa_net.audit import scan_agreement
    from coa_net.realdata import L93

    a = np.full((400, 400), 7, np.uint8)  # background index 7, roads drawn with index 3 along rows 100 and 300
    a[99:102, :] = 3
    a[299:302, :] = 3
    tr = from_origin(-2.5, 47.5 + 400 * 1e-4, 1e-4, 1e-4)
    with rasterio.open(tmp_path / "scan.tif", "w", driver="GTiff", width=400, height=400, count=1, dtype="uint8", crs="EPSG:4326",
                       transform=tr) as dst:
        dst.write(a, 1)
    lat = lambda row: 47.5 + (400 - row - 0.5) * 1e-4  # noqa: E731
    road = geopandas.GeoSeries([LineString([(-2.49, lat(100)), (-2.47, lat(100))])], crs="EPSG:4326").to_crs(L93).values
    ref = geopandas.GeoSeries([LineString([(-2.49, lat(300)), (-2.47, lat(300))])], crs="EPSG:4326").to_crs(L93).values
    off = geopandas.GeoSeries([LineString([(-2.49, lat(200)), (-2.47, lat(200))])], crs="EPSG:4326").to_crs(L93).values
    on, miss = scan_agreement(str(tmp_path / "scan.tif"), [road[0], off[0]], ref, tol_px=1, step_m=20)
    assert on == 1.0 and miss == 0.0


def test_implausible_slope_is_left_unknown():
    dx = 30.0 / (111_320 * math.cos(math.radians(47.5)))
    segs = [([(-2.5, 47.5), (-2.5 + dx, 47.5)], {"highway": "tertiary", "z": (50.0, 90.0), "z_prec": 1.0}, "both")]  # 133 %
    G = build_graph(segs, "t", simplify=False)
    d = next(iter(G.edges.values()))
    assert d.get("slope_pct") is None and d["slope_suspect"]


def test_order_terminal_in_closed_streets_moves_to_usable_road():
    from coa_net.corridors import CorridorGraph
    from coa_net.mission import build_context
    from coa_net.parsing import parse_order
    from coa_net.requirements import load_requirements

    step = 0.01
    segs = []
    for i in range(4):  # a 4 x 2 ladder of 6 m secondary roads
        segs.append(([(-2.5 + i * step, 47.5), (-2.5 + (i + 1) * step, 47.5)], {"highway": "secondary", "width_m": 6.0}, "both"))
        segs.append(([(-2.5 + i * step, 47.51), (-2.5 + (i + 1) * step, 47.51)], {"highway": "secondary", "width_m": 6.0}, "both"))
    for i in range(5):
        segs.append(([(-2.5 + i * step, 47.5), (-2.5 + i * step, 47.51)], {"highway": "secondary", "width_m": 6.0}, "both"))
    # the town label's node sits at the end of a 3 m street (too narrow for the 3.0 m PFM carrier plus margin)
    segs.append(([(-2.5, 47.5), (-2.5, 47.497)], {"highway": "secondary", "width_m": 3.0}, "both"))
    G = build_graph(segs, "t", simplify=False)
    town = G_node(G, -2.5, 47.497)
    G.nodes[town]["name"] = "Alpha"
    G.nodes[G_node(G, -2.46, 47.51)]["name"] = "Bravo"
    H = CorridorGraph(G)
    order = "ORDER T\nConvoy P: 2 x pfm_carrier from Alpha to Bravo, depart 06:00, priority 1.\n"
    ctx = build_context(H, parse_order(order, G), load_requirements())
    g = ctx.groups["P"]
    assert g.origin == G_node(G, -2.5, 47.5)  # the junction at the end of the narrow street, 333 m away
    assert any("origin Alpha moved 33" in n for n in ctx.notes)
    assert g.destination == G_node(G, -2.46, 47.51)  # already on the usable network: unchanged


def test_rounded_width_blocks_only_beyond_its_rounding():
    from coa_net.corridors import CorridorGraph
    from coa_net.costing import GroupCoster
    from coa_net.requirements import ActiveSet, load_requirements
    from coa_net.vehicles import Profile

    a, _ = _bd_attrs({"NATURE": "Route à 1 chaussée", "IMPORTANCE": "4", "LARGEUR": 3.0, "NB_VOIES": 1, "SENS": "Double"})
    assert a["width_m"] == 3.0 and a["width_prec_m"] == 0.5
    segs = [([(-2.5, 47.5), (-2.499, 47.5)], {"highway": "tertiary", "width_m": 3.0, "width_prec_m": 0.5}, "forward"),
            ([(-2.499, 47.5), (-2.498, 47.5)], {"highway": "tertiary", "width_m": 2.5, "width_prec_m": 0.25}, "forward")]
    G = build_graph(segs, "t", simplify=False)
    e = sorted(G.edges, key=lambda uv: G.nodes[uv[0]]["x"])
    gauge = [r for r in load_requirements() if r.template == "SEG.GAUGE" and r.params.get("check") == "width"]
    prof = Profile(ids=["P"], vehicle_classes={"heavy"}, cargo=set(), weight_t=32, mlc=32, width_m=3.0, height_m=3.9, length_m=12,
                   max_speed_kmh=70, max_slope_pct=15, tracked=False, vehicle_lengths=[12.0] * 6)
    c = GroupCoster(CorridorGraph(G), prof, ActiveSet(gauge))  # needs 3.0 + 0.5 m
    assert c.evals[e[0]].ok and "may be under" in c.evals[e[0]].unknown[0][1]  # 3 m may be 3.5 m: a priced risk
    assert not c.evals[e[1]].ok  # 2.5 m is at most 2.75 m: too narrow


def test_roadside_halts_only_when_the_data_has_none():
    """No halt area in the data: junctions of three or more main roads become halt candidates (not bridges)."""
    import networkx as nx

    from coa_net.realdata import roadside_halts

    G = nx.DiGraph()
    for u, v, rank in [(0, 1, 2), (0, 2, 3), (0, 3, 3), (4, 1, 5), (4, 5, 5), (4, 6, 3), (7, 2, 3), (7, 8, 3), (7, 9, 2)]:
        G.add_edge(u, v, road_rank=rank)
        G.add_edge(v, u, road_rank=rank)
    G[7][9]["bridge"] = G[9][7]["bridge"] = True
    notes = roadside_halts(G)
    assert [n for n, d in G.nodes(data=True) if d.get("halt_capacity")] == [0]  # 4: minor roads; 7: one leg is a bridge
    assert notes and "1 junctions" in notes[0] and "named RH-1 to RH-1" in notes[0]
    assert G.nodes[0]["halt_name"] == "RH-1" and G.nodes[0]["halt_roadside"]  # short: it labels every row of the march table
    assert roadside_halts(G) == []  # the graph now has halt areas: nothing more is added


def test_fast_halt_filter_matches_the_plain_distance_check(planned):
    """allowed_halt_nodes (vectorised, cached) keeps exactly the halt areas the element-by-element check keeps."""
    from coa_net.candidates import allowed_halt_nodes
    from coa_net.graph import node_xy, point_distance

    H = planned.H
    G = H.G
    for g in planned.ctx.groups.values():
        nodes = {n for n, d in G.nodes(data=True) if d.get("halt_capacity")}
        for r in g.active.of("PRO.CRITPOINT", ("T1",)):
            kinds, dmin = set(r.p("element_kinds")), r.p("no_stop_within_m")
            nodes = {n for n in nodes if all(point_distance(G, node_xy(G, n), e.xy) > dmin for e in H.elements.values() if e.kind in kinds)}
        assert allowed_halt_nodes(H, g) == nodes
        assert allowed_halt_nodes(H, g) == nodes  # second call from the cache

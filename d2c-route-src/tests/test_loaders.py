import networkx as nx

from d2c_route.loaders import osm_edge_attrs, osm_to_d2c, parse_number


def test_parse_number():
    assert parse_number("7.5 t") == 7.5
    assert parse_number("7500 kg") == 7.5
    assert parse_number("3,8") == 3.8
    assert parse_number("12'6\"") == round(12 * 0.3048 + 6 * 0.0254, 2)
    assert parse_number(None) is None and parse_number("default") is None


def test_osm_edge_attrs():
    a = osm_edge_attrs({"highway": "track", "tracktype": "grade3", "length": 120.0, "bridge": "yes", "maxweight": "20"})
    assert a["paved"] is False and a["bridge"] and a["max_weight_t"] == 20 and a["mlc"] == 20
    b = osm_edge_attrs({"highway": ["secondary", "tertiary"], "maxspeed": "FR:rural", "tunnel": "yes", "maxheight": "3.5"})
    assert b["paved"] is True and b["maxspeed_kmh"] == 80 and b["tunnel"] and b["max_height_m"] == 3.5
    c = osm_edge_attrs({"highway": "residential", "bridge": "no"})
    assert c["built_up"] and not c["bridge"] and c["mlc"] is None and c["width_m"] is None


def test_osm_to_d2c_keeps_shortest_parallel_edge():
    M = nx.MultiDiGraph()
    M.add_node(1, x=2.0, y=48.0)
    M.add_node(2, x=2.01, y=48.0)
    M.add_edge(1, 2, length=900.0, highway="primary")
    M.add_edge(1, 2, length=700.0, highway="secondary")
    G = osm_to_d2c(M)
    assert G.graph["crs"] == "EPSG:4326" and G[1][2]["length_m"] == 700.0

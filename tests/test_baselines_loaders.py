import networkx as nx

from coa_net.baselines import b0, b1, b2, b3
from coa_net.bench import network_metrics, run_incident_bench, run_planning_bench
from coa_net.loaders import osm_edge_attrs, osm_to_graph, parse_number


def test_baselines_on_the_demo(planned):
    prob = planned.prob
    H, crit = planned.H, planned.crit
    d0, d1, d2, d3 = b0(prob), b1(prob), b2(prob), b3(prob)
    assert not d0.asrs and not d1.asrs
    assert all(d2.asrs.get(g) is not None for g in d2.msr)
    m2, m3 = network_metrics(H, d2, crit), network_metrics(H, d3, crit)
    assert m3["reducible_spof_groups"] <= m2["reducible_spof_groups"]
    coa = network_metrics(H, planned.coas[planned.default].design, crit)
    assert coa["reducible_spof_groups"] == 0.0


def test_bench_runs_on_two_seeds():
    res = run_planning_bench(range(2), reps=5)
    assert set(res) >= {"B0", "B1", "B2", "B3", "COA-Net"}
    assert all(r["valid"] == 1.0 for r in res["COA-Net"])
    inc = run_incident_bench(range(2))
    assert all(r["valid"] == 1.0 for rows in inc.values() for r in rows)


def test_osm_attributes_and_level_crossings():
    assert parse_number("7500 kg") == 7.5 and parse_number("3.8 m") == 3.8 and parse_number("12'6\"") == 3.81
    a = osm_edge_attrs({"highway": "track", "tracktype": "grade3", "bridge": "yes", "maxweight": "12", "length": 50.0, "lit": "no"})
    assert a["paved"] is False and a["bridge"] and a["mlc"] == 12 and a["lit"] is False
    Gm = nx.MultiDiGraph()
    Gm.add_node(1, x=-1.6, y=48.1)
    Gm.add_node(2, x=-1.59, y=48.1, railway="level_crossing")
    Gm.add_node(3, x=-1.58, y=48.1)
    Gm.add_edge(1, 2, 0, length=700, highway="secondary", ford="yes")
    Gm.add_edge(2, 3, 0, length=800, highway="secondary")
    G = osm_to_graph(Gm)
    assert G[1][2]["ford"] and G[1][2]["level_crossing"] and G[2][3]["level_crossing"]
    assert G[2][3]["width_m"] is None  # unknown stays unknown

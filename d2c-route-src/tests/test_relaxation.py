import networkx as nx

from d2c_route.constraints import activate, load_rules
from d2c_route.graph import finalize_graph
from d2c_route.schemas import Convoy, LocationRef, MissionSpec, VehicleGroup
from d2c_route.vehicles import convoy_profile
from d2c_route.verifier import plan_verify_repair


def _line_graph(**track_attrs):
    """A - B - C where B-C is the only way and violates something."""
    G = nx.DiGraph(crs="local")
    for i, (n, name) in enumerate([(1, "A"), (2, "B"), (3, "C")]):
        G.add_node(n, x=i * 1000.0, y=0.0, name=name)
    base = dict(length_m=1000.0, highway="secondary", paved=True, width_m=6.0)
    for u, v, extra in [(1, 2, {}), (2, 3, track_attrs)]:
        G.add_edge(u, v, **{**base, **extra})
        G.add_edge(v, u, **{**base, **extra})
    return finalize_graph(G, centrality_samples=0)


def _mission(vtype):
    return MissionSpec(
        convoys=[Convoy(id="K", vehicles=[VehicleGroup(type=vtype, count=1)], origin=LocationRef(name="A"), destination=LocationRef(name="C"))]
    )


def _run(G, mission):
    rules = load_rules()
    profiles = {c.id: convoy_profile(c) for c in mission.convoys}
    prog = activate(rules, mission, profiles)
    plan, rep = plan_verify_repair(G, mission, prog, profiles, rules)
    return plan, rep, prog


def test_relaxable_rule_is_relaxed_and_reported():
    G = _line_graph(highway="track", paved=False)
    plan, rep, prog = _run(G, _mission("heavy_truck"))
    assert plan.convoys["K"].msr is not None
    assert "H-HEAVY-UNPAVED" in prog.convoys["K"].relaxed
    assert any(v.kind == "relaxed" and v.rule_id == "H-HEAVY-UNPAVED" for v in rep.violations)
    assert not rep.hard


def test_physical_rule_is_never_relaxed():
    G = _line_graph(bridge=True, mlc=20)
    plan, rep, prog = _run(G, _mission("tank_transporter"))
    cp = plan.convoys["K"]
    assert cp.msr is None and cp.status == "infeasible"
    assert any("operator decision needed" in m and "H-BRIDGE-MLC" in m for m in cp.messages)
    assert not prog.convoys["K"].relaxed


def test_unknown_data_is_priced_not_assumed():
    G = _line_graph(width_m=None)
    plan, rep, _ = _run(G, _mission("tank_transporter"))
    assert plan.convoys["K"].msr is not None
    # H-WIDTH treats a missing width as acceptable, but S-ROAD-CLASS still prices the road;
    # paved=None on a track is a genuine unknown for H-HEAVY-UNPAVED
    G = _line_graph(highway="track", paved=None, width_m=5.0)
    plan, rep, _ = _run(G, _mission("heavy_truck"))
    assert plan.convoys["K"].msr is not None
    assert any(v.kind == "unknown" and v.rule_id == "H-HEAVY-UNPAVED" for v in rep.violations)


def test_unclassified_bridge_is_forbidden_until_reconnoitred():
    G = _line_graph(bridge=True, mlc=None)
    plan, _, _ = _run(G, _mission("light_utility"))
    assert plan.convoys["K"].msr is None
    assert any("H-BRIDGE-MLC" in m for m in plan.convoys["K"].messages)

from d2c_route.graph import ukey
from d2c_route.scenarios import sample_text, synthetic_theatre
from d2c_route.pipeline import D2CRoute


def _keys(route):
    return {ukey(u, v) for u, v in zip(route.nodes[:-1], route.nodes[1:])}


def test_bridge_destroyed_local_repair(session):
    before = {cid: cp.msr.nodes for cid, cp in session.plan.convoys.items()}
    plan, rr, rep = session.incident("Pont-Est bridge destroyed", 365)
    closed = {ukey(u, v) for u, v in rr.edited_edges}
    assert closed and all(session.G.edges[e]["closed"] for e in rr.edited_edges)
    assert "C2" in rr.affected  # C2 crossed Pont-Est
    assert "C1" not in rr.affected and plan.convoys["C1"].msr.nodes == before["C1"]
    for cid, cp in plan.convoys.items():
        assert cp.msr is not None
        # the part still to drive avoids the closed bridge
        future = {ukey(s.u, s.v) for s in cp.schedule if s.t_in >= 365}
        assert not (future & closed), cid
    assert rep.metrics["hard_violations"] == 0 and rep.metrics["conflicts"] == 0


def test_repair_keeps_driven_prefix(session):
    old = session.plan.convoys["C2"]
    plan, rr, _ = session.incident("Pont-Est bridge destroyed", 365)
    new = plan.convoys["C2"]
    driven = [s for s in old.schedule if s.t_in < 365]
    assert [(s.u, s.v) for s in new.schedule[: len(driven)]] == [(s.u, s.v) for s in driven]
    assert new.msr.nodes[0] == old.msr.nodes[0]


def test_threat_zone_raises_threat_and_breakdown_delays(session):
    plan, rr, rep = session.incident("Enemy ambush confirmed near Ville-Centre, 1500 m radius", 372)
    assert any(session.G.edges[e]["threat"] > 0 for e in rr.edited_edges)
    arrival = plan.convoys["C3"].arrival_min
    plan, rr, rep = session.incident("Convoy C3 breakdown, 25 minutes", 380)
    assert rr.affected == ["C3"] and plan.convoys["C3"].arrival_min >= arrival + 25 - 1e-6


def test_local_repair_is_more_stable_than_full():
    order = sample_text("sample_order.txt")
    shares = {}
    for mode in ("local", "full"):
        s = D2CRoute(synthetic_theatre(0).G)
        s.plan_mission(order)
        _, rr, _ = s.incident("Pont-Est bridge destroyed", 365, mode=mode)
        shares[mode] = (rr.unchanged_share, len(rr.affected))
    assert shares["local"][1] <= shares["full"][1]
    assert shares["local"][0] >= shares["full"][0]

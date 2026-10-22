from d2c_route.constraints import activate, load_rules
from d2c_route.expr import eval_predicate
from d2c_route.graph import resolve, ukey
from d2c_route.optimizer import overlap
from d2c_route.schemas import Convoy, LocationRef, MissionSpec, VehicleGroup
from d2c_route.vehicles import convoy_profile


def _edges(route):
    return list(zip(route.nodes[:-1], route.nodes[1:]))


def test_seed_rules_load_and_scope():
    rules = load_rules()
    mission = MissionSpec(
        phase="night",
        convoys=[
            Convoy(id="F", vehicles=[VehicleGroup(type="fuel_tanker", count=2)], origin=LocationRef(name="Alpha"), destination=LocationRef(name="Delta")),
            Convoy(id="L", vehicles=[VehicleGroup(type="light_utility", count=2)], origin=LocationRef(name="Alpha"), destination=LocationRef(name="Delta")),
        ],
    )
    profiles = {c.id: convoy_profile(c) for c in mission.convoys}
    prog = activate(rules, mission, profiles)
    f, l = prog.convoys["F"], prog.convoys["L"]
    assert "H-HAZMAT-TUNNEL" in {r.id for r in f.hard} and "H-HAZMAT-TUNNEL" not in {r.id for r in l.hard}
    assert "H-HEAVY-UNPAVED" in {r.id for r in f.hard} and "H-HEAVY-UNPAVED" not in {r.id for r in l.hard}
    assert f.temporal["speed_factor"] == 0.7  # night
    assert f.temporal["max_speed_kmh"] == 50  # stricter heavy cap wins over the general 60
    assert "S-COVER" not in {t.rule_id for t in f.soft}  # day-only rule


def test_initial_plan_is_valid_and_respects_planted_constraints(session):
    G = session.G
    assert session.report.ok, session.report.summary()
    assert session.report.metrics["hard_violations"] == 0
    assert session.report.metrics["conflicts"] == 0
    for cid, cp in session.plan.convoys.items():
        veh = session.profiles[cid].as_vehicle()
        for u, v in _edges(cp.msr):
            d = G[u][v]
            if d["bridge"] and d["mlc"] is not None:
                assert d["mlc"] >= veh["mlc"], (cid, d["name"])
            if d["tunnel"]:
                assert d["max_height_m"] >= veh["height_m"] + 0.3
    # C1 carries fuel (MLC 35): the MLC 30 bridge on the straight line is avoided
    c1 = session.plan.convoys["C1"]
    assert not any(G[u][v]["bridge"] and G[u][v]["mlc"] == 30 for u, v in _edges(c1.msr))


def test_alternates_respect_network_rules(session):
    for cid, cp in session.plan.convoys.items():
        net = session.program.convoys[cid].network
        assert len(cp.asrs) >= net["min_alternates"]
        for a in cp.asrs:
            assert overlap(session.G, a, cp.msr) <= net["max_overlap"] + 1e-9


def test_deconfliction_keeps_headway(session):
    head = 10
    occ = {}
    for cid, cp in session.plan.convoys.items():
        for s in cp.schedule:
            occ.setdefault(ukey(s.u, s.v), []).append((s.t_in, s.t_out, cid))
    for slots in occ.values():
        for i, (a_in, a_out, a) in enumerate(slots):
            for b_in, b_out, b in slots[i + 1 :]:
                if a != b:
                    assert b_in >= a_out + head - 1e-6 or a_in >= b_out + head - 1e-6


def test_verifier_catches_doctrine_blind_route(session):
    """The shortest path for C1 crosses the MLC 30 bridge: the verifier must flag it."""
    from d2c_route.optimizer import shortest
    from d2c_route.schemas import Route

    G = session.G
    c1 = session.mission.convoys[0]
    sp = shortest(G, resolve(G, c1.origin), resolve(G, c1.destination), "length_m")
    veh = session.profiles["C1"].as_vehicle()
    breaks = [
        r.id for u, v in zip(sp[:-1], sp[1:]) for r in session.program.convoys["C1"].hard if eval_predicate(r.predicate, G[u][v], veh) is False
    ]
    assert "H-BRIDGE-MLC" in breaks
    from d2c_route.verifier import verify

    bad = session.plan.model_copy(deep=True)
    bad.convoys["C1"].msr = Route(nodes=sp, length_m=0, travel_time_min=0, cost=0)
    rep = verify(G, bad, session.program, session.profiles, session.mission, session.rules)
    assert any(v.rule_id == "H-BRIDGE-MLC" and v.convoy_id == "C1" for v in rep.hard)


def test_explanation_cites_rules(session):
    text = session.explain()
    assert "H-BRIDGE-MLC" in text and "Rejected" in text and "ASR1" in text

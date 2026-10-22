import json

from d2c_route.llm import ScriptedLLM
from d2c_route.parsing import compile_doctrine, parse_incident, parse_order
from d2c_route.scenarios import sample_text
from d2c_route.schemas import IncidentType


def test_order_pattern_parser(theatre):
    m = parse_order(sample_text("sample_order.txt"), theatre.G)
    assert m.id == "LOG-01" and m.phase == "day"
    assert [c.id for c in m.convoys] == ["C1", "C2", "C3"]
    c1 = m.convoys[0]
    assert {(g.type, g.count) for g in c1.vehicles} == {("heavy_truck", 6), ("fuel_tanker", 2)}
    assert c1.earliest_departure_min == 360 and c1.latest_arrival_min == 510
    assert "fuel" in c1.cargo and c1.origin.name == "Alpha" and c1.destination.name == "Delta"


def test_order_french_and_unknown_vehicle(theatre):
    m = parse_order("Ordre X\nConvoi K1: 2 x char_lourd de Alpha vers Delta, départ 07h30, priorité 2", theatre.G, {"heavy_truck": None})
    assert m.convoys[0].earliest_departure_min == 450 and m.convoys[0].priority == 2
    assert any("char_lourd" in a for a in m.ambiguities)


def test_incident_pattern_parser(theatre):
    G = theatre.G
    i = parse_incident("FLASH - Pont-Est bridge destroyed by artillery", G)
    assert i.type == IncidentType.BRIDGE_DESTROYED and i.location.name == "Pont-Est"
    i = parse_incident("Embuscade ennemie confirmée près de Ville-Centre, rayon 1500 m", G)
    assert i.type == IncidentType.THREAT_ZONE and i.radius_m == 1500 and i.severity >= 0.8
    i = parse_incident("Convoy C3 breakdown, 25 minutes", G)
    assert i.type == IncidentType.BREAKDOWN and i.convoy_id == "C3" and i.duration_min == 25


def test_doctrine_fallback_compiler():
    rules, log = compile_doctrine(sample_text("sample_doctrine.txt"))
    preds = {r.predicate for r in rules if r.predicate}
    assert "not edge.bridge or edge.mlc >= vehicle.mlc" in preds
    assert any(r.params.get("min_headway_min") == 10 for r in rules)
    assert any(abs(r.params.get("speed_factor", 0) - 0.7) < 1e-9 for r in rules)


def test_doctrine_llm_output_is_validated():
    def responder(stage, user):
        return {
            "rules": [
                {"type": "hard", "predicate": "edge.mlc >= vehicle.mlc", "rationale": "ok"},
                {"type": "hard", "predicate": "__import__('os').system('x')", "rationale": "malicious"},
                {"type": "network", "params": {"teleport": 1}, "rationale": "bad key"},
                {"type": "hard", "predicate": "edge.mlc >= vehicle.mlc", "rationale": "duplicate"},
            ]
        }

    llm = ScriptedLLM(responder)
    rules, log = compile_doctrine("Some doctrine.", llm=llm)
    assert [r.predicate for r in rules] == ["edge.mlc >= vehicle.mlc"]
    assert sum("rejected" in line for line in log) == 2 and any("duplicate" in line for line in log)
    assert llm.usage.calls == 1 and llm.usage.by_stage["doctrine"]["calls"] == 1


def test_order_llm_failure_falls_back(theatre):
    llm = ScriptedLLM(lambda stage, user: {"not": "a mission"})
    m = parse_order(sample_text("sample_order.txt"), theatre.G, llm=llm)
    assert len(m.convoys) == 3 and any("fallback" in a for a in m.ambiguities)


def test_order_llm_success(theatre):
    spec = {
        "id": "M",
        "phase": "night",
        "convoys": [
            {"id": "X", "vehicles": [{"type": "medium_truck", "count": 3}], "origin": {"name": "Bravo"}, "destination": {"name": "Delta"}}
        ],
    }
    m = parse_order("whatever", theatre.G, llm=ScriptedLLM(lambda s, u: json.loads(json.dumps(spec))))
    assert m.phase == "night" and m.convoys[0].id == "X"

"""Parsers, intent mapper, doctrine compiler and the guards on model outputs (every model step has a fallback)."""

from coa_net.doctrine import compile_doctrine
from coa_net.intent import keyword_intent, map_intent
from coa_net.llm import ScriptedLLM, unsupported
from coa_net.orders import PlanDiff, UnitChange, write_frago
from coa_net.parsing import grading_from_text, parse_incident, parse_order, parse_time
from coa_net.scenarios import sample_text, synthetic_theatre
from coa_net.schemas import IncidentType
from coa_net.vehicles import load_catalogue


def test_order_parser_fallback():
    G = synthetic_theatre(0).G
    m = parse_order(sample_text("sample_order.txt"), G)
    assert [c.id for c in m.convoys] == ["C1", "C2", "C3"]
    c1 = m.convoys[0]
    assert c1.earliest_departure_min == 360 and c1.latest_arrival_min == 510 and c1.cargo == ["fuel", "rations"]
    assert "crossings" in m.intent and m.phase == "day"
    assert not m.ambiguities


def test_order_parser_flags_unknown_vehicles_and_reads_avoid():
    G = synthetic_theatre(0).G
    text = ("Phase: night\nAvoid Ville-Centre.\nConvoy C1: 3 x hovercraft from Alpha to Delta, depart 22:00, priority 1.\n"
            "Convoy C2: 2 x medium_truck from Alpha to Delta, depart 22:10, priority 2.")
    m = parse_order(text, G, load_catalogue())
    assert m.phase == "night"
    assert any("hovercraft" in a for a in m.ambiguities)  # flagged, never guessed
    assert [loc.name for loc in m.avoid] == ["Ville-Centre"]


def test_incident_parser_and_grading():
    G = synthetic_theatre(0).G
    i = parse_incident("FLASH 06:05 - Pont-Est bridge destroyed by artillery fire (graded A1).", G, "I-1")
    assert i.type == IncidentType.BRIDGE_DESTROYED and i.location.name == "Pont-Est" and i.time_min == 365
    assert (i.reliability, i.credibility) == ("A", 1)
    assert grading_from_text("ambush confirmed near the bridge") == ("B", 1)
    assert grading_from_text("possible roadblock") == ("B", 3)
    assert parse_time("06h30") == 390


def test_intent_mapper_on_the_sample_order():
    r = keyword_intent("Deliver the fuel to Delta before 08:30. The enemy is likely to strike the crossings. Keep the tankers out of the towns.")
    assert r.order[:2] == ["resilience", "hazmat_builtup"]
    assert sorted(r.order) == sorted(set(r.order)) and len(r.order) == 9
    assert any("deadline" in n for n in r.notes)


def test_intent_model_output_is_checked():
    text = "Speed is essential. Avoid the towns."
    bad = ScriptedLLM(lambda system, user: {"order": ["travel_time"], "quotes": {"travel_time": "we must hurry"}})
    r = map_intent(text, bad)
    assert r.source == "keywords" and any("rejected" in n for n in r.notes)
    menu = ["travel_time", "builtup", "threat_exposure", "resilience", "doctrine", "hazmat_builtup", "network_size", "cover", "preferences"]
    good = ScriptedLLM(lambda system, user: {"order": menu, "quotes": {"travel_time": "Speed is essential."}})
    r = map_intent(text, good)
    assert r.source == "model" and r.order[0] == "travel_time"


def test_doctrine_compiler_coverage_and_quotes():
    text = sample_text("sample_doctrine.txt")
    reqs, rep = compile_doctrine(text, "sample")
    assert rep.coverage >= 0.9
    assert reqs and all(r.status == "draft" and r.source.quote for r in reqs)
    norm = " ".join(text.split()).lower()
    assert all(" ".join(r.source.quote.split()).lower() in norm for r in reqs)


def test_unsupported_numbers_and_identifiers():
    facts = "C2: reroute FALCON; arrival 06:50; 11.5 km"
    assert unsupported("C2 takes FALCON, arriving 06:50 after 11.5 km.", facts) == []
    bad = unsupported("C2 takes FALCON, arriving 07:10 after 12 km on MSR TIGER.", facts)
    assert "07:10" in bad and "12" in bad and "TIGER" in bad


def test_frago_falls_back_to_the_template_when_the_model_invents():
    diff = PlanDiff(1, "LOG-01", "Pont-Est destroyed (report graded A1).", [UnitChange("C2", "C2", "reroute", route="FALCON", arrive=410.0)])
    liar = ScriptedLLM(lambda s, u: {"frago": "1. Situation. x 2. Mission. 3. Execution. C2 arrives 07:30. 4. Sustainment. 5. Command and signal."})
    text, src = write_frago(diff, liar)
    assert src == "template" and "06:50" in text and "07:30" not in text
    honest = ScriptedLLM(lambda s, u: {"frago": "1. Situation. Pont-Est destroyed. 2. Mission. No change. 3. Execution. C2: FALCON, arrival 06:50. "
                                                "4. Sustainment. No change. 5. Command and signal. No change."})
    text, src = write_frago(diff, honest)
    assert src == "model"

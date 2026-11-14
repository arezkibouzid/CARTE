"""Orders written as text -> scenario patch (offline: named points of the PDF and coordinates only)."""

from __future__ import annotations

from coa_net.llm import ScriptedLLM
from interface.backend import text_order as T

EX = [
    {"number": 1, "points": {"PD1": [4.81, 48.95], "PA1": [5.16, 48.77], "DA1": [4.0, 49.2], "DA2": [5.5, 49.3], "DA3": [5.6, 48.7], "DA4": [4.7, 48.6]},
     "start": "PD1", "end": "PA1", "waypoints": [], "waypoint_order": "fixed", "objective": "length", "round_trip": False,
     "speed_cap_kmh": 80.0, "area": ["DA1", "DA2", "DA3", "DA4"], "requirements": []},
    {"number": 7, "points": {"PD4": [5.41, 49.26], "PA4": [4.87, 48.70], "WP1": [5.27, 49.11], "WP2": [4.67, 49.10], "WP3": [5.25, 48.91]},
     "start": "PD4", "end": "PA4", "waypoints": ["WP1", "WP2", "WP3"], "waypoint_order": "free", "objective": "length",
     "round_trip": False, "speed_cap_kmh": 80.0, "area": [], "requirements": []},
]


def test_rules_full_order():
    r = T.parse("De PD4 à PA4 par WP1 et WP3 dans n'importe quel ordre, le plus rapide, 60 km/h, éviter 2 km autour de WP2", EX, use_llm=False)
    sc, op = r["scenario"], r["options"]
    assert sc["start"] == [5.41, 49.26] and sc["end"] == [4.87, 48.70]
    assert sc["waypoints"] == [[5.27, 49.11], [5.25, 48.91]]  # WP2 is avoided, not visited
    assert sc["waypointOrder"] == "free" and op == {"objective": "time", "speedCap": 60}
    (zone,) = sc["avoid"]
    assert min(p[0] for p in zone) < 4.67 < max(p[0] for p in zone) and min(p[1] for p in zone) < 49.10 < max(p[1] for p in zone)
    assert r["method"] == "rules" and not r["unresolved"]


def test_exercise_then_override_and_coordinates():
    r = T.parse("Exercice 7 mais le plus rapide à 60 km/h", EX, use_llm=False)
    assert r["scenario"]["waypoints"] == [[5.27, 49.11], [4.67, 49.10], [5.25, 48.91]] and r["options"]["objective"] == "time"
    r = T.parse("Va de 49.10, 5.27 vers PA1 en aller-retour", EX, use_llm=False)
    assert r["scenario"]["start"] == [5.27, 49.1] and r["scenario"]["roundTrip"] is True


def test_place_names_and_unknown_points_are_reported_not_guessed():
    r = T.parse("Convoi de PD1 vers Verdun en évitant Dugny, puis PD9", EX, use_llm=False)
    assert "end" not in r["scenario"] and "avoid" not in r["scenario"]
    assert "Verdun" in r["unresolved"] and "Dugny" in r["unresolved"] and any("PD9" in u for u in r["unresolved"])


def test_chat_questions_are_not_commands():
    assert T.parse("Pourquoi le segment 3 passe par WP2 ?", EX, mode="auto", use_llm=False) == {"is_command": False}
    assert T.parse("peux-tu calculer de PD1 à PA1 ?", EX, mode="auto", use_llm=False)["is_command"]


def _model_answer(**kw):
    base = {"start": "none", "end": "none", "waypoints": [], "waypoint_order": "unspecified", "round_trip": "unspecified",
            "objective": "unspecified", "speed_kmh": 0, "avoid": [], "deployment_area": "unspecified", "exercise": 0, "unresolved_places": []}
    return base | kw


def test_model_answer_is_used_when_checked(monkeypatch):
    from interface.backend import orders as O

    llm = ScriptedLLM(lambda stage, user: _model_answer(start="PD1", end="PA1", objective="time", speed_kmh=70, round_trip="yes"))
    llm.model = "scripted"
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    r = T.parse("je veux partir de PD1 et arriver à PA1 au plus vite, 70 km/h max, puis revenir par une autre route", EX)
    assert r["method"] == "model" and r["options"] == {"objective": "time", "speedCap": 70} and r["scenario"]["roundTrip"] is True


def test_model_hallucination_is_rejected(monkeypatch):
    from interface.backend import orders as O

    # the model maps "Verdun" to PA1 and invents a speed: both fail the checks, the rules are used instead
    llm = ScriptedLLM(lambda stage, user: _model_answer(start="PD1", end="PA1", speed_kmh=90))
    llm.model = "scripted"
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    r = T.parse("Convoi de PD1 vers Verdun", EX)
    assert r["method"] == "rules" and "end" not in r["scenario"] and "Verdun" in r["unresolved"]
    assert any("PA1" in p for p in r["model_problems"]) and any("90" in p for p in r["model_problems"])


MAP = {"start": [5.0, 49.0], "end": [4.9, 48.8], "waypoints": [[5.1, 48.9], [5.2, 48.95]], "pointed": [4.95, 49.05],
       "avoid": [[[4.7, 49.0], [4.8, 49.0], [4.8, 48.95], [4.7, 48.95]]], "rect": [4.5, 48.7, 5.3, 49.2], "area": None}


def test_map_references():
    r = T.parse("Va d'ici à PA1 par l'étape 2, évite 3 km autour du départ", EX, use_llm=False, mapctx=MAP)
    sc = r["scenario"]
    assert sc["start"] == [4.95, 49.05] and sc["end"] == [5.16, 48.77] and sc["waypoints"] == [[5.2, 48.95]]
    (zone,) = sc["avoid"]  # a square around the departure placed on the map
    assert min(p[0] for p in zone) < 5.0 < max(p[0] for p in zone)
    r = T.parse("De PD1 à PA1 dans le rectangle, évite la zone 1", EX, use_llm=False, mapctx=MAP)
    assert r["scenario"]["area"][0] == [4.5, 48.7] and "avoid" not in r["scenario"] and any("déjà évitée" in n for n in r["notes"])
    r = T.parse("Évite le rectangle, va ici", EX, use_llm=False, mapctx=MAP)
    assert r["scenario"]["avoid"][0][2] == [5.3, 49.2] and r["scenario"]["end"] == [4.95, 49.05]


def test_missing_map_objects_are_reported():
    r = T.parse("Va d'ici à l'étape 5", EX, use_llm=False, mapctx={"waypoints": [], "avoid": []})
    assert not r["changes"] and any("point P" in u for u in r["unresolved"]) and any("étape 5" in u for u in r["unresolved"])


def test_model_settings_need_words_and_distinct_points(monkeypatch):
    from interface.backend import orders as O

    # the model adds « fastest » that no word says (dropped) and makes P both departure and arrival (rejected)
    llm = ScriptedLLM(lambda stage, user: _model_answer(start="P", end="P", objective="time"))
    llm.model = "scripted"
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    r = T.parse("va ici", EX, mapctx=MAP)
    assert r["method"] == "rules" and r["scenario"] == {"end": [4.95, 49.05]} and "options" in r and not r["options"]

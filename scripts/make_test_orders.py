"""Synthetic linked orders, to test and demonstrate how the app handles dependencies between orders.

    .venv/bin/python scripts/make_test_orders.py        (writes outputs/orders/extractions/test_*.json)

FICTIONAL test data, not from any real document: an OPORD, two FRAGOs that amend it (the second dated later), an annex
that completes it with a conflicting load limit, and an independent order. Same extraction schema (3.2) as the real
orders, marked ``_amiad.synthetic = true`` so the interface labels them « TEST synthétique ».
"""
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "outputs" / "orders" / "extractions"


def fact(i, page, text, kind="constraint", category="other", quantities=()):
    return {"id": f"P{page:03d}_S{i:03d}", "text": text, "page": page,
            "quantities": [{"value": v, "unit": u, "printed_text": f"{v:g} {u}"} for v, u in quantities],
            "annotation": {"kind": kind, "category": category, "scope": None, "availability": "not_applicable"}}


def order(oid, title, facts, desc):
    return oid, {"schema_version": "3.2", "exercise_id": oid.upper(), "document": {"id": oid, "filename": f"{oid}.pdf (synthétique)"},
                 "main_pages": [1], "context_pages": [], "task_types": ["route_planning"], "description": desc,
                 "facts": facts, "ignored_fragments_preserved": [], "printed_coordinates": [], "issues": [], "visual_notes": [],
                 "evidence": [{"id": f["id"], "page": 1, "role": "main", "modality": "text", "quote": f["text"]} for f in facts],
                 "review_status": "synthetic_test_data", "manual_correction": False,
                 "_amiad": {"status": "synthetic_test", "synthetic": True, "title": title, "model": "données de test (pas d’extraction)"}}


ORDERS = [
    order("test_opord7", "OPORD 7/2026", [
        fact(1, 1, "OPORD 7/2026 — ORDRE D'OPÉRATION (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        fact(2, 1, "DTG 140800Z NOV 26", "other", "other"),
        fact(3, 1, "Le convoi rejoint PA4 depuis PD4 par l'itinéraire le plus court.", "objective", "route"),
        fact(4, 1, "Vitesse maximale du convoi : 60 km/h", "constraint", "speed", [(60, "km/h")]),
        fact(5, 1, "Espacement entre véhicules : 100 m", "constraint", "spacing", [(100, "m")]),
        fact(6, 1, "Charge maximale des véhicules : 40 tonnes", "constraint", "load", [(40, "tonnes")]),
        fact(7, 1, "Halte de 15 min toutes les 2 h", "constraint", "time", [(15, "min"), (2, "h")]),
    ], "Ordre de base (test)."),
    order("test_frago1", "FRAGO 1 à l'OPORD 7/2026", [
        fact(1, 1, "FRAGO 1 à l'OPORD 7/2026 (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        fact(2, 1, "DTG 141400Z NOV 26", "other", "other"),
        fact(3, 1, "Le présent FRAGO modifie l'OPORD 7/2026 suite à l'endommagement d'un pont.", "constraint", "other"),
        fact(4, 1, "Vitesse maximale du convoi ramenée à 50 km/h", "constraint", "speed", [(50, "km/h")]),
        fact(5, 1, "Espacement entre véhicules porté à 150 m", "constraint", "spacing", [(150, "m")]),
    ], "Modification 1 (test)."),
    order("test_frago2", "FRAGO 2 à l'OPORD 7/2026", [
        fact(1, 1, "FRAGO 2 à l'OPORD 7/2026 (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        fact(2, 1, "DTG 141800Z NOV 26", "other", "other"),
        fact(3, 1, "Modifie l'OPORD 7/2026 : vitesse maximale du convoi abaissée à 40 km/h.", "constraint", "speed", [(40, "km/h")]),
    ], "Modification 2 (test)."),
    order("test_annexe_b", "Annexe B à l'OPORD 7/2026", [
        fact(1, 1, "ANNEXE B (logistique) à l'OPORD 7/2026 (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        fact(2, 1, "Complète l'OPORD 7/2026 pour les véhicules citernes.", "constraint", "other"),
        fact(3, 1, "Charge maximale des véhicules : 35 tonnes", "constraint", "load", [(35, "tonnes")]),
    ], "Annexe (test) : contredit la charge de l'OPORD sans la modifier."),
    order("test_ordre_x", "OPORD 9/2026 (indépendant)", [
        fact(1, 1, "OPORD 9/2026 — ORDRE D'OPÉRATION (DONNÉES DE TEST SYNTHÉTIQUES)", "other", "other"),
        fact(2, 1, "DTG 150600Z NOV 26", "other", "other"),
        fact(3, 1, "Vitesse maximale du convoi : 70 km/h", "constraint", "speed", [(70, "km/h")]),
    ], "Ordre indépendant (test)."),
]

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for oid, x in ORDERS:
        (OUT / f"{oid}.json").write_text(json.dumps(x, ensure_ascii=False, indent=1), encoding="utf-8")
        print("wrote", OUT / f"{oid}.json")

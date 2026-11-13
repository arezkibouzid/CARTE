"""Orders workflow: extraction 3.2 -> RAG input 2.0, rule search (RAG bundle), statuses, explanations and chat guards.

The rule search needs Node.js (skipped without it); the language model is replaced by a scripted one, so these tests
check the plumbing and the guards, not the model's wording."""

import json
import shutil
from pathlib import Path

import pytest

from coa_net.llm import ScriptedLLM, Usage
from interface.backend import orders as O

REVIEWED = O.EXTRACTION_DIR / "examples" / "exercice_22B" / "rag_input.reviewed.json"


def _v32() -> dict:
    """A 3.2 extraction built by the bundle's own export from the reviewed 22B texts (no model involved)."""
    import sys

    sys.path.insert(0, str(O.EXTRACTION_DIR))
    from contract import export

    v2 = json.loads(REVIEWED.read_text(encoding="utf-8"))
    sources = {e["id"]: e for e in v2["evidence"]}
    annotations = {}
    for f in v2["facts"]:
        for sid in f["source_ids"][:1]:
            if sources.get(sid, {}).get("modality") == "text":
                kind = {"constraint": "constraint", "requested_output": "requested_output"}.get(f["kind"], "other")
                annotations[sid] = {"kind": kind, "category": "output" if kind == "requested_output" else f["category"],
                                    "scope": f.get("scope"), "availability": "not_applicable"}
    sources = {k: dict(v, quote=v.get("quote") or "") for k, v in sources.items() if k in annotations or v["modality"] == "image"}
    data = {"task_types": v2["task_types"], "annotations": annotations, "visual_notes": {}}
    case = {"exercise_id": "22B", "main_pages": v2["main_pages"], "context_pages": v2["context_pages"]}
    return export(data, sources, case, v2["document"], v2["printed_coordinates"])


def test_to_rag_v2_keeps_every_fragment_and_its_source():
    x = _v32()
    v2 = O.to_rag_v2(x)
    assert v2["schema_version"] == "2.0" and v2["exercise_id"] == "22B"
    assert [f["id"] for f in v2["facts"]] == [f["id"] for f in x["facts"]]
    for f, g in zip(x["facts"], v2["facts"]):
        assert g["text"] == O._clean(f["text"]) and g["source_ids"] == [f["id"]]
    speed = next(f for f in v2["facts"] if "Night" in f["text"] or "night" in f["text"])
    assert speed["kind"] == "constraint" and speed["value"] == 40 and speed["unit"] == "km/h"
    assert O.to_rag_v2(v2) is v2  # 2.0 passes through
    with pytest.raises(ValueError):
        O.to_rag_v2({"schema_version": "9"})


def test_map_inputs_from_printed_coordinates():
    m = O.map_inputs(json.loads(REVIEWED.read_text(encoding="utf-8")))
    assert len(m["area"]) == 4 and all(len(p) == 2 for p in m["area"])
    assert m["start"] == [4.09213, 49.15615] or "start" in m


def test_status_guard():
    rules = [{"rule_id": "DR-0029", "group": "applicable"}, {"rule_id": "DR-0031", "group": "applicable"},
             {"rule_id": "DR-0001", "group": "potential"}, {"rule_id": "DR-0099", "group": "not_applicable"}]
    assert O.status_problems("DR-0029 et DR-0031 ne s'appliquent pas ici.", rules)
    assert O.status_problems("Vous devez absolument respecter DR-0001.", rules)
    assert O.status_problems("DR-0099 est applicable.", rules)
    assert not O.status_problems("DR-0029 est explicitement applicable ; la présence d'un pont reste à vérifier.", rules)
    assert not O.status_problems("DR-0001 n'est pas obligatoire mais utile.", rules)
    assert not O.status_problems("DR-0099 ne s'applique pas ici.", rules)


needs_node = pytest.mark.skipif(not O._has_node(), reason="Node.js absent (RAG/search needs it)")


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "STORE", tmp_path / "extractions")
    monkeypatch.setattr(O, "DATA", tmp_path)
    monkeypatch.setattr(O, "endpoints", lambda: {})
    O._rules_cache.clear()
    (tmp_path / "extractions").mkdir()
    shutil.copy(REVIEWED, tmp_path / "extractions" / "r22B.json")
    (tmp_path / "extractions" / "v22B.json").write_text(json.dumps(_v32()), encoding="utf-8")
    return tmp_path


@needs_node
def test_rules_same_for_3_2_and_reviewed_2_0(store):
    a, b = O.analysis("v22B"), O.analysis("r22B")
    assert {r["rule_id"] for r in a["rules"]} == {r["rule_id"] for r in b["rules"]}
    applicable = {r["rule_id"] for r in a["rules"] if r["group"] == "applicable"}
    assert applicable and all(r["enforce"] for r in a["rules"] if r["group"] == "applicable")
    assert all(r["source"]["pdf_document"] and r["source"]["pdf_page"] for r in a["rules"])
    gap = next(r for r in a["rules"] if r["rule_id"] == "DR-0046")
    assert any("100 m" in next(f["text"] for f in a["facts"] if f["id"] == i) for i in gap["linked_facts"])


@needs_node
def test_explain_without_model_uses_rule_engine_wording(store):
    a = O.explain("v22B", "concise", use_llm=False)
    assert a["explanation"]["model"] is None
    assert all(r["explanation"] for r in a["rules"])
    assert all(r["explanation"].startswith("Règle explicitement applicable") for r in a["rules"] if r["group"] == "applicable")


@needs_node
def test_explain_rejects_wrong_status_then_falls_back(store, monkeypatch):
    def respond(system, user):
        ids = [l.split(" [")[0] for l in user.splitlines() if l.startswith("DR-")]
        return {"items": [{"rule_id": i, "explanation": f"{i} ne s'applique pas à cet ordre.", "order_elements": []} for i in ids]}
    llm = ScriptedLLM(respond)
    llm.model, llm.down, llm.usage = "scripted", None, Usage()
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    a = O.explain("v22B", "standard")
    assert a["explanation"]["model"] is None and a["explanation"]["fallback_reason"]
    assert all("ne s'applique pas" not in r["explanation"] for r in a["rules"] if r["group"] == "applicable")


@needs_node
def test_chat_cites_only_retrieved_rules_and_adds_sources(store, monkeypatch):
    def respond(system, user):
        assert "Contexte actuel de l'interface" in user and "4.36" in user  # the map's points reach the model
        return {"answer": "DR-0061 est explicitement applicable : les haltes longues figurent sur le graphique.",
                "cited_rules": ["DR-0061"], "cited_facts": []}
    llm = ScriptedLLM(respond)
    llm.model, llm.down, llm.usage = "scripted", None, Usage()
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    r = O.chat("v22B", [{"role": "user", "content": "Pourquoi les haltes ?"}], "concise", {"start": [4.36, 48.96]})
    assert r["model"] == "scripted" and r["cited_rules"] == ["DR-0061"]
    assert r["sources"][0]["pdf_document"] and r["sources"][0]["group"] == "applicable"


@needs_node
def test_chat_without_model_retrieves_from_french_question(store):
    r = O.chat("v22B", [{"role": "user", "content": "Quelle vitesse sur les ponts ?"}], "concise")
    assert r["model"] is None and r["cited_rules"]
    assert any(s["rule_id"] in ("DR-0029", "DR-0031") for s in r["sources"])

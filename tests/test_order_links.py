"""Dependencies between orders: links, values in force along a chain, conflicts, outdated values rejected.

Uses the synthetic orders of scripts/make_test_orders.py (fictional) in a temporary store."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from interface.backend import order_links as L
from interface.backend import orders as O

spec = importlib.util.spec_from_file_location("mk", Path(__file__).resolve().parents[1] / "scripts" / "make_test_orders.py")
mk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mk)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    st = tmp_path / "extractions"
    st.mkdir()
    for oid, x in mk.ORDERS:
        (st / f"{oid}.json").write_text(json.dumps(x, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(O, "STORE", st)
    monkeypatch.setattr(O, "corpus", lambda: {"cases": []})
    monkeypatch.setattr(L, "LINKS_FILE", tmp_path / "order_links.json")
    L._cache.update(key=None, headers=None)
    return st


def test_headers_and_links(store):
    h = L.header("test_frago1")
    assert h["ref"] == "FRAGO 1" and h["date"].startswith("2026-11-14T14:00")
    rel = {(l["from"], l["to"]): l["relation"] for l in L.links()}
    assert rel == {("test_frago1", "test_opord7"): "amends", ("test_frago2", "test_opord7"): "amends",
                   ("test_annexe_b", "test_opord7"): "complements"}


def test_values_in_force_conflicts_and_independent_orders(store):
    r = L.resolve(["test_frago2", "test_ordre_x"])
    assert set(r["added"]) == {"test_opord7", "test_frago1", "test_annexe_b"}  # the chain comes in by itself
    eff = {e["key"]: e for e in r["effective"] if e["in_force"]["order"] != "test_ordre_x"}  # the independent order has its own
    assert eff["speed_max"]["in_force"]["printed"] == "40 km/h"  # FRAGO 2 is later than FRAGO 1
    assert {x["printed"] for x in eff["speed_max"]["replaced"]} == {"60 km/h", "50 km/h"}
    assert eff["spacing"]["in_force"]["printed"] == "150 m"
    assert [k["key"] for k in r["conflicts"]] == ["load_max"]  # the annex completes, it does not amend
    assert any(k["key"] == "speed_max" and {v["printed"] for v in k["values"]} == {"40 km/h", "70 km/h"} for k in r["per_order"])
    full, short = L.context_block(r)
    assert "40 km/h" in full and "Conflits" in full and "remplace" in short


def test_operator_links(store):
    L.set_link("test_ordre_x", "test_opord7", "replaces", "add")
    L._cache.update(key=None)
    r = L.resolve(["test_opord7"])
    assert any(v["order"] == "test_opord7" and v["relation"] == "replaces" for v in r["void"])
    L.set_link("test_annexe_b", "test_opord7", None, "reject")
    L._cache.update(key=None)
    assert ("test_annexe_b", "test_opord7") not in {(l["from"], l["to"]) for l in L.links()}


def test_outdated_value_is_rejected(store):
    r = L.resolve(["test_frago2"])
    assert L.stale_problems("La vitesse maximale du convoi est de 60 km/h.", r)
    assert not L.stale_problems("La vitesse maximale est de 40 km/h ; elle remplace les 60 km/h de l’OPORD.", r)
    assert not L.stale_problems("La vitesse maximale est de 40 km/h.", r)


@pytest.mark.skipif(shutil.which("node") is None and not (Path.home() / ".local/node/bin/node").exists(), reason="Node.js (RAG) absent")
def test_orders_chat_rejects_an_outdated_answer(store, monkeypatch, tmp_path):
    from coa_net.llm import ScriptedLLM

    monkeypatch.setattr(O, "DATA", tmp_path)
    llm = ScriptedLLM(lambda stage, user: {"answer": "La vitesse maximale du convoi est de 60 km/h.", "cited_rules": [], "cited_facts": []})
    llm.model = "scripted"
    monkeypatch.setattr(O, "llm_client", lambda: llm)
    r = O.chat("test_frago2", [{"role": "user", "content": "Quelle est la vitesse maximale ?"}])
    assert r["model"] is None and "40 km/h (test_frago2" in r["answer"] and r["links"]["conflicts"] == 1

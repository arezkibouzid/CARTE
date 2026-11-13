"""Uncertainty-aware route cost, segment trace and route chat on a small synthetic network (no raster, no model).

    A ──(short road, the model is unsure: p = 0.1)── M ── B
    └──────────(longer road, sure: p = 0.75)── C ──────┘

With lambda = 0 the short road wins; with a strong uncertainty cost the solver pays the detour to stay on the sure road.
"""
import math

import networkx as nx
import numpy as np
import pytest

from coa_net import pdfexercises as px
from coa_net.route_explain import explain_route, trace_text

P = {"A": (5.00, 49.00), "M": (5.05, 49.00), "B": (5.10, 49.00), "C": (5.05, 49.03)}
SURE, UNSURE = 0.75, 0.10


def _km(a, b):
    (x0, y0), (x1, y1) = P[a], P[b]
    return math.hypot((x1 - x0) * 73.0, (y1 - y0) * 111.2)


def network():
    G = nx.DiGraph()
    for n, (x, y) in P.items():
        G.add_node(n, x=x, y=y)
    for a, b in (("A", "M"), ("M", "B"), ("A", "C"), ("C", "B")):
        for u, v in ((a, b), (b, a)):
            G.add_edge(u, v, coords=(P[u], P[v]), length_m=_km(u, v) * 1000, road_rank=3, highway="unclassified", source_id="unet")
    N = px.Network(G, "test", priced=False)
    N.base_price = N.price.copy()
    unsure = np.array([("M" in e) for e in N.edges])
    owner = np.arange(len(N.edges))
    pc = np.where(unsure, UNSURE, SURE)
    return N, owner, pc


def exercise():
    pts = {"PD": P["A"], "PA": P["B"], "DA1": (4.9, 49.1), "DA2": (5.2, 49.1), "DA3": (5.2, 48.9), "DA4": (4.9, 48.9)}
    return px.PdfExercise(number=0, pages=[], points=pts, area=["DA1", "DA2", "DA3", "DA4"], start="PD", end="PA", waypoints=[],
                          waypoint_order="none", objective="length", round_trip_distinct=False, speed_cap_kmh=80, legal_speed=True,
                          requirements=[], outputs=[], description="")


def solve(lam):
    N, owner, pc = network()
    N.price = N.base_price.copy()
    info = N.price_by_confidence(owner, pc, lam, p_ref=SURE)
    return N, px.solve(exercise(), N), info


def used_nodes(N, res):
    return {N.nodes[int(N.u[i])] for le in res["_leg_edges"] for i in le} | {N.nodes[int(N.v[i])] for le in res["_leg_edges"] for i in le}


def test_risk_is_minus_log_normalised_confidence():
    N, owner, pc = network()
    N.price_by_confidence(owner, pc, 2.0, p_ref=SURE)
    unsure = np.array([("M" in e) for e in N.edges])
    assert np.allclose(N.risk[~unsure], 0.0)
    assert np.allclose(N.risk[unsure], -math.log(UNSURE / SURE))
    assert np.allclose(N.price[unsure], 1 + 2.0 * -math.log(UNSURE / SURE))
    assert np.allclose(N.confidence[unsure], UNSURE / SURE)


def test_uncertainty_cost_flips_the_route():
    N0, r0, _ = solve(0.0)
    N1, r1, _ = solve(3.0)
    assert "M" in used_nodes(N0, r0) and "C" not in used_nodes(N0, r0)
    assert "C" in used_nodes(N1, r1) and "M" not in used_nodes(N1, r1)
    assert r1["total"]["length_km"] > r0["total"]["length_km"]
    assert r1["legs"][0]["mean_confidence"] > r0["legs"][0]["mean_confidence"]
    # the solver also reports the route it would take without the uncertainty term
    assert r1["unpriced_optimum"]["same_route"] is False


def test_trace_gives_alternative_and_verdict():
    N, res, _ = solve(0.0)
    tr = explain_route(N, res, "length", 80)
    segs = [s for l in tr["legs"] for s in l["segments"]]
    assert segs and sum(s["km"] for s in segs) == pytest.approx(res["total"]["length_km"], abs=0.05)
    s = segs[0]
    assert s["alternative"] is not None and s["alternative"]["extra_km"] > 0
    assert s["verdict"] in ("clearly best", "narrow choice")
    assert f"{s['alternative']['km']:.1f}" in s["why"]
    assert "Critères" in trace_text(tr)


def test_trace_with_uncertainty_lists_unpriced_alternative():
    N, res, _ = solve(3.0)
    tr = explain_route(N, res, "length", 80)
    kinds = [a["kind"] for a in tr["alternatives"]]
    assert "without uncertainty cost" in kinds
    alt = next(a for a in tr["alternatives"] if a["kind"] == "without uncertainty cost")
    assert alt["confidence"] < [s for l in tr["legs"] for s in l["segments"]][0]["confidence"]


def test_trace_answers_without_model():
    from interface.backend import route_chat as RC

    N, res, _ = solve(0.0)
    tr = explain_route(N, res, "length", 80)
    entry = {"trace": tr, "total": res["total"], "network": "test", "compare": {}}
    n = len([s for l in tr["legs"] for s in l["segments"]])
    out = RC.chat(entry, [{"role": "user", "content": "Explique le segment 1"}], use_llm=False)
    assert out["mode"] == "trace" and out["cited_segments"] == ["L1S1"]
    out = RC.chat(entry, [{"role": "user", "content": f"Pourquoi le segment {n + 5} ?"}], use_llm=False)
    assert "n'existe pas" in out["answer"]
    out = RC.chat(entry, [{"role": "user", "content": "Quelles alternatives ont été rejetées ?"}], use_llm=False)
    assert out["mode"] == "trace"
    # a question the trace does not answer, without a model: the trace summary, said as such
    out = RC.chat(entry, [{"role": "user", "content": "Le convoi peut-il rouler de nuit ?"}], use_llm=False)
    assert out["mode"] == "trace (repli)" and "sans modèle" in out["answer"]
    assert out["context"]["used_tokens"] <= out["context"]["budget_tokens"]


def test_context_packer_budget():
    from interface.backend.route_chat import ContextPacker, tokens

    P_ = ContextPacker(100)
    P_.add("q", "question", required=True)
    P_.add("big", "x" * 3000, "y" * 60)
    P_.add("huge", "z" * 9000)
    txt = P_.text()
    assert P_.report["shortened"] == ["big"] and P_.report["omitted"] == ["huge"]
    assert tokens(txt) <= 100


def test_scenarios_are_never_overwritten(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from interface.backend import amiad_api as A

    monkeypatch.setattr(A, "SCENARIOS", tmp_path)
    ids = [A.scenarios_save(A.ScenarioDoc(name="same name", scenario={"k": i}))["id"] for i in range(2)]
    assert len(set(ids)) == 2 and len(list(tmp_path.glob("*.json"))) == 2
    assert A.scenarios_get(ids[0])["scenario"] == {"k": 0}
    assert [x["name"] for x in A.scenarios_list()] == ["same name", "same name"]
    with pytest.raises(HTTPException):
        A.scenarios_get("../../pyproject")


def test_rule_questions_with_orders_skip_the_trace_path(monkeypatch):
    from interface.backend import route_chat as RC

    N, res, _ = solve(0.0)
    tr = explain_route(N, res, "length", 80)
    entry = {"trace": tr, "total": res["total"], "network": "test", "compare": {}}
    monkeypatch.setattr(RC.O, "analysis", lambda oid, policy="lenient": {"facts": [], "rules": [], "exercise_id": oid, "objective": None})
    out = RC.chat(entry, [{"role": "user", "content": "Quelle règle de vitesse pour le segment 1 ?"}], order_ids=["a", "b"], use_llm=False)
    blocks = [b["block"] for b in out["context"]["blocks"]]
    assert out["mode"] == "trace (repli)" and blocks[:3] == ["question", "itinéraire (trace du solveur)", "règles des ordres"]

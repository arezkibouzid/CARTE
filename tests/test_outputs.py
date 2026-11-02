"""COA briefing, explanations, rendering, report and the no-solver path."""

import json

from coa_net.coa import briefing, template_comparison
from coa_net.llm import ScriptedLLM
from coa_net.pipeline import COANet
from coa_net.render import md_to_html, render_map, render_movement
from coa_net.scenarios import sample_incidents, sample_text, synthetic_theatre
from coa_net.parsing import parse_time


def test_facts_table_and_templated_comparison(planned):
    assert len(planned.facts) >= 2
    default = next(f for f in planned.facts if f.name == planned.default)
    assert default.irreducible == ["C2: Pont-Est"]
    assert any("bridge col 9" in r for r in default.recce)
    text, concerns = template_comparison(planned.facts, planned.default)
    assert "follows the confirmed order" in text
    assert any("Pont-Est" in c for c in concerns)


def test_facts_list_the_junctions_every_route_must_pass(planned):
    """On Scenario 1 the facts row "Irreducible SPOFs" read "none" while the verification of the same COA counted one
    irreducible point: a junction every route of C1 passes (an articulation point), left out of the facts."""
    import dataclasses

    from coa_net.coa import facts_table

    H = planned.H
    c = planned.coas[planned.default]
    r = c.design.msr["C1"]
    node = H.arc_end(r.arcs[0])
    added = f"AP-{node}" not in H.elements
    ap = H.add_articulation(node).id
    try:
        d = dataclasses.replace(c.design, msr={**c.design.msr, "C1": dataclasses.replace(r, elements=r.elements | {ap})},
                                irreducible={**c.design.irreducible, "C1": set(c.design.irreducible.get("C1", set())) | {ap}})
        f = facts_table(H, planned.ctx, [d], {d.name: c.ref}, planned.crit)[0]
        assert f"C1: {H.elements[ap].name}" in f.irreducible and "C2: Pont-Est" in f.irreducible
    finally:
        if added:
            H.elements.pop(ap)


def test_briefing_model_text_must_use_the_facts(planned):
    liar = ScriptedLLM(lambda s, u: {"comparison": "COA I saves 42 minutes.", "concerns": []})
    text, concerns, src = briefing(planned.facts, planned.default, liar)
    assert src == "template"


def test_contrastive_explanations(planned):
    ex = planned.coas[planned.default].explanations
    text = "\n".join(c.sentence for c in ex)
    assert "C1 does not take the fastest road route" in text and "hazmat_builtup" in text
    spof = next(c for c in ex if "irreducible single point of failure" in c.subject)
    assert "Pont-Est" in spof.subject and "RTE.DETOUR" in spof.reason
    hold = [c for c in ex if c.subject.startswith("C3 holds")]
    assert hold and "SCH.HEADWAY" in hold[0].reason


def test_explanations_share_the_road_route_between_coas(planned, monkeypatch):
    """The doctrine-free road route of a group is the same for every COA: computed once per group (on Scenario 1 each
    computation evaluated all 553,313 roads again), with the same explanations as without sharing."""
    from coa_net import explain

    plain = {n: [x.sentence for x in explain.explain_plan(planned.H, planned.ctx, planned.prob, c.design, c.sched, c.ref)]
             for n, c in planned.coas.items()}
    calls = []
    real = explain.road_route
    monkeypatch.setattr(explain, "road_route", lambda H, g: calls.append(g.id) or real(H, g))
    cache: dict = {}
    for n, c in planned.coas.items():
        shared = explain.explain_plan(planned.H, planned.ctx, planned.prob, c.design, c.sched, c.ref, road_cache=cache)
        assert [x.sentence for x in shared] == plain[n]
    assert len(planned.coas) > 1 and sorted(calls) == sorted(set(calls)) == sorted(cache)


def test_svg_renderers(planned):
    c = planned.coas[planned.default]
    svg = render_map(planned.H, c.ref, c.design, planned.crit, title="t")
    assert svg.startswith("<svg") and all(nm in svg for nm in c.ref.routes)
    mv = render_movement(planned.H, c.sched, c.design.msr["C3"], c.ref)
    assert "<polygon" in mv and "C3" in mv
    html = md_to_html("# T\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- x\n- y")
    assert "<table>" in html and "<li>x</li>" in html


def test_reconnaissance_timing_in_the_facts(planned):
    """On Scenario 1 every reconnaissance task read "not in time" and became a concern, including the tasks not valued
    (beyond the cap) and those no plan depends on."""
    from types import SimpleNamespace as T

    from coa_net.coa import recce_status

    assert recce_status(T(feasible=True, answers=[1], deadline=400.0, affected=["C1"])) == "in time"
    assert recce_status(T(feasible=False, answers=[1], deadline=400.0, affected=["C1"])) == "not in time"
    assert recce_status(T(feasible=False, answers=[], deadline=None, affected=[])) == "not valued"
    assert recce_status(T(feasible=False, answers=[1], deadline=None, affected=[])) == "no deadline: no plan depends on it"
    assert recce_status(T(feasible=False, answers=[1], deadline=None, affected=["C2"])) == "no deadline: affected groups already moving"
    for f in planned.facts:
        tasks = {t.id: t for t in planned.coas[f.name].recce}
        assert len(f.recce) == len(tasks)
        for r in f.recce:
            assert r.endswith(f"({recce_status(tasks[r.split()[0]])})")
    _, concerns = template_comparison(planned.facts, planned.default)
    n_late = sum(1 for c in planned.coas.values() for t in c.recce if recce_status(t) == "not in time")
    assert sum(1 for c in concerns if "reconnaissance" in c) <= n_late


def test_large_maps_draw_the_main_roads_merged(planned, monkeypatch):
    """Scenario 1's graph has 287,836 roads: one <line> per road made each map 27 MB. Above LARGE_MAP_ROADS the map
    draws the main roads merged into one path per style and names only the bridges of the designated routes."""
    from coa_net import render

    c = planned.coas[planned.default]
    full = render_map(planned.H, c.ref, c.design, planned.crit)
    monkeypatch.setattr(render, "LARGE_MAP_ROADS", 10)
    large = render_map(planned.H, c.ref, c.design, planned.crit)
    assert large.startswith("<svg") and len(large) < len(full)
    assert large.count("<line") < full.count("<line") / 4 and "<path d=\"M" in large
    assert all(nm in large for nm in c.ref.routes) and "roads of rank 4 or better" in large
    on_routes = {e for g in c.design.msr for r in c.design.routes(g) for e in r.elements}
    bridges = [el for e, el in planned.H.elements.items() if el.kind == "bridge"]
    off = [el.name for el in bridges if el.id not in on_routes]
    on = [el.name for el in bridges if el.id in on_routes]
    assert off and on
    assert all(f">{n} MLC" in full for n in off + on)
    assert all(f">{n} MLC" not in large for n in off) and all(f">{n} MLC" in large for n in on)


def test_end_to_end_without_solver_or_model(tmp_path):
    s = COANet.from_graph(synthetic_theatre(0).G, use_cpsat=False, sim_reps=5, explain=False,
                          data_notes=["no halt area in the data: roadside halt candidates"])
    coas = s.plan(sample_text("sample_order.txt"))
    assert coas and all(c.design.solver == "greedy" for c in coas)
    assert all(c.sched.solver.startswith("greedy") for c in coas)
    s.choose()
    for i in sample_incidents():
        o = s.incident(i["text"], parse_time(i["time"]))
        assert o.mode in ("playbook", "repair", "reschedule", "logged")
    page = s.report(tmp_path / "out")
    assert page.exists() and (tmp_path / "out" / "network.geojson").exists()
    assert "Data and assumptions" in page.read_text() and "roadside halt candidates" in page.read_text()  # not only on the console
    m = json.loads((tmp_path / "out" / "metrics.json").read_text())
    assert len(m["incidents"]) == 3
    assert "FRAGO 01" in (tmp_path / "out" / "fragos.txt").read_text()

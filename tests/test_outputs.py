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


def test_svg_renderers(planned):
    c = planned.coas[planned.default]
    svg = render_map(planned.H, c.ref, c.design, planned.crit, title="t")
    assert svg.startswith("<svg") and all(nm in svg for nm in c.ref.routes)
    mv = render_movement(planned.H, c.sched, c.design.msr["C3"], c.ref)
    assert "<polygon" in mv and "C3" in mv
    html = md_to_html("# T\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n- x\n- y")
    assert "<table>" in html and "<li>x</li>" in html


def test_end_to_end_without_solver_or_model(tmp_path):
    s = COANet.from_graph(synthetic_theatre(0).G, use_cpsat=False, sim_reps=5, explain=False)
    coas = s.plan(sample_text("sample_order.txt"))
    assert coas and all(c.design.solver == "greedy" for c in coas)
    assert all(c.sched.solver.startswith("greedy") for c in coas)
    s.choose()
    for i in sample_incidents():
        o = s.incident(i["text"], parse_time(i["time"]))
        assert o.mode in ("playbook", "repair", "reschedule", "logged")
    page = s.report(tmp_path / "out")
    assert page.exists() and (tmp_path / "out" / "network.geojson").exists()
    m = json.loads((tmp_path / "out" / "metrics.json").read_text())
    assert len(m["incidents"]) == 3
    assert "FRAGO 01" in (tmp_path / "out" / "fragos.txt").read_text()

from coa_net.belief import BeliefState, Report, report_probability
from coa_net.simulate import SimIncident, monte_carlo, plan_from, run_once
from coa_net.scenarios import synthetic_theatre

from conftest import element_id


def test_grading_table():
    assert abs(report_probability("A", 1) - 0.95) < 1e-9
    assert abs(report_probability("B", 2) - 0.8) < 1e-9
    assert abs(report_probability("C", 3) - 0.5) < 1e-9


def test_noisy_or_fusion_and_decay():
    G = synthetic_theatre(0).G
    e = next(iter(G.edges))
    b = BeliefState(G, half_life_min=60, closed_p=0.5, suspect_p=0.2)
    b.add(Report("r1", "closure", [e], 0.4, 0.0, confirmed=False))
    assert b.status(e, 0.0) == "suspect"
    b.add(Report("r2", "closure", [e], 0.4, 0.0, confirmed=False))
    assert abs(b.closure_p(e, 0.0) - 0.64) < 1e-9 and b.status(e, 0.0) == "closed"
    assert b.status(e, 120.0) == "logged"  # two unconfirmed reports, two half-lives later: 1-(1-0.1)^2 = 0.19
    b.clear([e], 130.0)
    assert b.status(e, 131.0) == "clear"


def test_threshold_at_exactly_the_suspect_probability():
    G = synthetic_theatre(0).G
    e = next(iter(G.edges))
    b = BeliefState(G)
    b.add(Report("r", "closure", [e], 0.4, 0.0, confirmed=False))
    assert b.status(e, 60.0) == "suspect"  # 0.4 x 2^-1 = 0.2, despite floating-point error


def test_playbooks_cover_the_crossings_of_the_network(planned):
    c = planned.coas[planned.default]
    H = planned.H
    els = {pb.element for pb in c.playbooks}
    assert element_id(H, "Pont-Est") in els and element_id(H, "Pont-Ouest") in els
    pb = next(pb for pb in c.playbooks if pb.element == element_id(H, "Pont-Est"))
    assert pb.responses["C3"].status == "designated"  # C3 switches to its ASR
    assert pb.responses["C2"].status in ("fallback", "stranded")  # irreducible single point of failure
    assert "FRAGO" in pb.frago and "3. Execution" in pb.frago


def test_reconnaissance_of_bridge_col_9_ranks_first(planned):
    c = planned.coas[planned.default]
    assert c.recce, "a reconnaissance task is expected"
    t = c.recce[0]
    assert t.name == "bridge col 9" and t.feasible
    assert t.level == "resilience"  # valued first on the top objective of the confirmed order
    assert t.removes_spof.get("C2", 0.0) > 0.0  # the answer may remove C2's single point of failure


def test_playbooks_reduce_columns_reaching_a_lost_bridge(planned):
    H, ctx = planned.H, planned.ctx
    c = planned.coas[planned.default]
    with_pb = plan_from(H, ctx, c.design, c.sched, c.playbooks, name="with")
    without = plan_from(H, ctx, c.design, c.sched, None, name="without")
    script = [SimIncident("closure", 365, element=element_id(H, "Pont-Est"), label="I-1")]
    a = monte_carlo(H, ctx, with_pb, script, reps=60, seed=1)
    b = monte_carlo(H, ctx, without, script, reps=60, seed=1)
    assert a.reached_closed() <= b.reached_closed()
    assert sum(a.weighted_delay(r) for r in a.reps) <= sum(b.weighted_delay(r) for r in b.reps) + 1e-6


def test_common_random_numbers_make_runs_repeatable(planned):
    H, ctx = planned.H, planned.ctx
    c = planned.coas[planned.default]
    p = plan_from(H, ctx, c.design, c.sched, c.playbooks)
    r1 = run_once(H, ctx, p, [], rep=3, seed=7)
    r2 = run_once(H, ctx, p, [], rep=3, seed=7)
    assert {k: u.arrival for k, u in r1.units.items()} == {k: u.arrival for k, u in r2.units.items()}

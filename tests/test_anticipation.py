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


def test_derived_coster_matches_a_fresh_one(planned):
    from coa_net.graph import ukey
    from coa_net.mission import build_context

    H, ctx = planned.H, planned.ctx
    bridge = next(el for el in H.elements.values() if el.kind == "bridge")
    ov = {ukey(*k): {"mlc": 10} for k in bridge.edges}
    fresh = build_context(H, ctx.mission, planned.requirements, overrides=ov)
    derived = build_context(H, ctx.mission, planned.requirements, overrides=ov, base=ctx)
    for gid, g in fresh.groups.items():
        d = derived.groups[gid].coster
        assert d is not ctx.groups[gid].coster
        assert {e: (v.ok, round(v.time_min, 9), v.blocked) for e, v in g.coster.evals.items()} == \
               {e: (v.ok, round(v.time_min, 9), v.blocked) for e, v in d.evals.items()}
        assert {a: (v.ok, round(v.time_min, 9)) for a, v in g.coster.arcs.items()} == {a: (v.ok, round(v.time_min, 9)) for a, v in d.arcs.items()}
        assert ctx.groups[gid].coster.overrides != d.overrides or not ov


def test_shared_what_if_worlds_give_the_same_reconnaissance(planned):
    """The plan shares the what-if worlds of the value of information between its COAs: same tasks as without."""
    from coa_net.anticipation import value_of_information

    def key(tasks):
        return [(t.element, t.attribute, t.level, round(t.value, 6), {k: round(v, 6) for k, v in t.values.items()}, t.feasible)
                for t in tasks]

    H, ctx = planned.H, planned.ctx
    worlds: dict = {}
    sizes = []
    for c in planned.coas.values():
        args = (H, ctx, planned.requirements, c.design, planned.crit)
        kw = dict(ref=c.ref, use_cpsat=planned.use_cpsat, max_elements=planned.recce_max_elements, rebase=True)  # as the plan
        fresh = value_of_information(*args, **kw)
        shared = value_of_information(*args, **kw, world_cache=worlds)
        assert key(fresh) == key(shared) == key(c.recce)
        sizes.append(len(worlds))
    assert sizes[0] > 0 and sizes[-1] < sizes[0] * len(sizes)  # later COAs reuse worlds already planned


def test_unaffected_groups_keep_their_candidates_in_what_if_worlds(planned, monkeypatch):
    """Reusing the base candidates of groups an answer does not affect gives the same tasks with fewer searches."""
    from coa_net import anticipation

    calls = {"n": 0}
    real = anticipation.generate_candidates

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(anticipation, "generate_candidates", counting)
    c = planned.coas[planned.default]
    args = (planned.H, planned.ctx, planned.requirements, c.design, planned.crit)
    kw = dict(ref=c.ref, use_cpsat=planned.use_cpsat, max_elements=planned.recce_max_elements)
    full = anticipation.value_of_information(*args, **kw)
    n_full, calls["n"] = calls["n"], 0
    reused = anticipation.value_of_information(*args, **kw, base_cands=planned.cands)
    key = lambda ts: [(t.element, t.level, round(t.value, 6), {k: round(v, 6) for k, v in t.values.items()}, t.feasible) for t in ts]
    assert key(full) == key(reused)
    assert 0 < calls["n"] < n_full


def test_answers_that_change_no_road_keep_the_plan(planned, monkeypatch):
    """On Scenario 1 COA R credited six tunnels with the same value for every answer, "too low for every vehicle"
    included: the what-if plans, solved with 5 s per solver call, were compared with the COA's own design, solved with
    10 s. The plan is now compared with a re-solve by the same procedure, and an answer that leaves every group's roads
    as they are keeps that plan without a new solve."""
    from coa_net import anticipation

    solved = []
    real = anticipation.solve
    monkeypatch.setattr(anticipation, "solve", lambda *a, **k: solved.append(1) or real(*a, **k))
    c = planned.coas[planned.default]
    tasks = anticipation.value_of_information(planned.H, planned.ctx, planned.requirements, c.design, planned.crit, ref=c.ref,
                                              use_cpsat=planned.use_cpsat, max_elements=planned.recce_max_elements, rebase=True)
    answers = [a for t in tasks for a in t.answers]
    assert answers and len(solved) <= len(answers)  # one solve for the plan without the answer, none for at least one answer
    low = next(a for t in tasks if t.attribute == "mlc" for a in t.answers if a.label.startswith("MLC <"))
    assert not low.changes and low.objectives == low.baseline  # a bridge too weak for every group: closed, as without the answer

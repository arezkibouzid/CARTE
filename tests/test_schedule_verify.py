from coa_net.schedule import headway_min, march_table, schedule
from coa_net.simulate import plan_from, run_once
from coa_net.verify import verify

from conftest import element_id


def _check_headways(planned, sched):
    ctx = planned.ctx
    cs = list(sched.credits.values())
    for a in cs:
        for b in cs:
            if a.unit >= b.unit:
                continue
            for la in a.legs:
                for lb in b.legs:
                    if la.arc != lb.arc:
                        continue
                    first, second = (la, lb) if la.t_in <= lb.t_in else (lb, la)
                    lead = a if first is la else b
                    h = headway_min(ctx.groups[(b if lead is a else a).group])
                    assert second.t_in >= first.t_in + first.pass_min + h - 0.11, (a.unit, b.unit, la.arc)


def test_movement_credits_keep_the_time_gap(planned):
    sched = planned.coas[planned.default].sched
    assert sched.solver == "cp-sat"
    _check_headways(planned, sched)
    c3 = sched.credits["C3"]
    assert c3.hold > 0  # C3 leaves Alpha behind C1 on the shared road


def test_greedy_scheduler_fallback_keeps_the_time_gap(planned):
    d = planned.coas[planned.default].design
    sched = schedule(planned.H, planned.ctx, d.msr, use_cpsat=False)
    assert sched.solver.startswith("greedy")
    _check_headways(planned, sched)


def test_deterministic_replay_has_no_conflict(planned):
    c = planned.coas[planned.default]
    rep = run_once(planned.H, planned.ctx, plan_from(planned.H, planned.ctx, c.design, c.sched), [], deterministic=True)
    assert rep.conflicts == 0
    for uid, u in rep.units.items():
        assert abs(u.arrival - c.sched.credits[uid].arrive) < 0.5


def test_demo_plan_is_valid_and_names_its_irreducible_spof(planned):
    c = planned.coas[planned.default]
    assert c.report.ok, [f.__dict__ for f in c.report.violations]
    irr = [f for f in c.report.findings if f.status == "irreducible"]
    assert any("Pont-Est" in f.detail or "Pont-Est" in f.subject for f in irr)
    rows = march_table(planned.H, c.sched, c.ref)
    assert {r["point"] for r in rows} >= {"SP", "RP"}


def test_verifier_catches_a_forbidden_bridge(planned):
    H, ctx = planned.H, planned.ctx
    d = planned.coas[planned.default].design
    pc = element_id(H, "Pont-Centre")
    # C1 (MLC 35) forced over Pont-Centre (MLC 30): pick any C3 candidate through it, re-made for C1
    from coa_net.candidates import allowed_halt_nodes, make_route

    g = ctx.groups["C1"]
    blind = next(r for r in planned.cands["C3"].routes if pc in r.elements)
    bad = make_route(H, g, blind.arcs, set(), allowed_halt_nodes(H, g))
    from dataclasses import replace

    d2 = replace(d, msr={**d.msr, "C1": bad})
    rep = verify(H, ctx, d2, None, None, planned.crit)
    assert not rep.ok
    assert any(f.req == "R-01" and f.status == "violation" for f in rep.findings)

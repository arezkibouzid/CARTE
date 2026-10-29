from coa_net.candidates import generate_candidates
from coa_net.design import DEFAULT_EPS, DEFAULT_ORDER, solve

from conftest import element_id


def _crossing_names(H, r):
    return {H.elements[e].name for e in r.elements if H.elements[e].kind in ("bridge", "tunnel")}


def test_vehicle_requirements_shape_the_candidates(planned):
    H = planned.H
    for r in planned.cands["C2"].routes:  # tank transporters, MLC 100
        assert "Pont-Centre" not in _crossing_names(H, r)  # MLC 30
        assert "bridge col 9" not in _crossing_names(H, r)  # unknown class, forbidden for bridges
    tunnel = next(e for e, el in H.elements.items() if el.kind == "tunnel")
    for r in planned.cands["C1"].routes:  # fuel tankers: SEG.CARGO T1, no tunnel
        assert tunnel not in r.elements


def test_pont_est_is_irreducible_for_c2_only(planned):
    H, crit = planned.H, planned.crit
    pe = element_id(H, "Pont-Est")
    assert pe in crit.irreducible["C2"]
    assert pe not in crit.irreducible.get("C3", set())
    rep = planned.cands["C2"].replacement[pe]
    assert rep is not None and rep.time_min / planned.cands["C2"].best_time > 1.8  # beyond the detour guard


def _survivable(H, d, crit):
    for g, m in d.msr.items():
        for e in m.elements:
            if e in crit.irreducible.get(g, set()) or (g, e) in d.waived:
                continue
            assert any(e not in a.elements for a in d.asrs.get(g, [])), (g, H.elements[e].name)


def test_every_coa_is_survivable_and_meets_the_detour_guard(planned):
    H, crit = planned.H, planned.crit
    for c in planned.coas.values():
        d = c.design
        _survivable(H, d, crit)
        for g, alts in d.asrs.items():
            for a in alts:
                assert a.time_min <= 1.8 * planned.cands[g].best_time + 1e-6


def test_default_coa_follows_the_intent(planned):
    d = planned.coas[planned.default].design
    assert d.order[:3] == ["lateness", "resilience", "hazmat_builtup"]
    assert d.objectives["hazmat_builtup"] == 0.0  # "keep the tankers out of the towns"
    H = planned.H
    assert "Pont-Ouest" in _crossing_names(H, d.msr["C1"])
    assert any("Pont-Est" in _crossing_names(H, a) for a in d.asrs["C1"])  # an alternate over another bridge


def test_greedy_fallback_is_survivable_too(planned):
    prob = planned.prob
    d = solve(prob, list(DEFAULT_ORDER), dict(DEFAULT_EPS), "G", use_cpsat=False)
    assert d.solver == "greedy" and set(d.msr) == {"C1", "C2", "C3"}
    _survivable(planned.H, d, planned.crit)


def test_lexicographic_levels_are_respected(planned):
    prob = planned.prob
    fast = solve(prob, ["travel_time", *[k for k in DEFAULT_ORDER if k != "travel_time"]], {"travel_time": 0.0}, "F")
    res = solve(prob, ["resilience", *[k for k in DEFAULT_ORDER if k != "resilience"]], {"resilience": 0.0}, "R")
    assert fast.objectives["travel_time"] <= res.objectives["travel_time"] + 1e-6
    assert res.objectives["resilience"] <= fast.objectives["resilience"] + 1e-6


def test_candidates_from_a_current_position(planned):
    g = planned.ctx.groups["C3"]
    msr = planned.coas[planned.default].design.msr["C3"]
    mid = msr.dps(planned.H)[2]
    cs = generate_candidates(planned.H, g, origin=mid, seed_routes=[tuple(msr.arcs[2:])])
    assert cs.routes and all(r.nodes[0] == mid for r in cs.routes)
    assert any(r.key == tuple(msr.arcs[2:]) for r in cs.routes)  # the remaining route stays in the pool

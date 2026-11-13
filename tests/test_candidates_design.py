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


def test_two_unit_flow_matches_network_simplex(planned):
    """The successive-shortest-path solver behind disjoint_pair finds the same optimum as networkx's network simplex."""
    import random

    import networkx as nx

    from coa_net.candidates import _flow_network, _two_unit_min_cost_flow

    def nx_cost(net, s, t):
        F = nx.DiGraph()
        for (a, b), (c, w) in net.items():
            F.add_edge(a, b, capacity=c, weight=w)
        F.nodes[s]["demand"], F.nodes[t]["demand"] = -2, 2
        try:
            return nx.cost_of_flow(F, nx.min_cost_flow(F))
        except nx.NetworkXUnfeasible:
            return None

    def cost(net, flow):
        return None if flow is None else sum(f * net[e][1] for e, f in flow.items())

    for g in planned.ctx.groups.values():
        net = _flow_network(planned.H, g.coster)
        assert cost(net, _two_unit_min_cost_flow(net, g.origin, g.destination)) == nx_cost(net, g.origin, g.destination)
    rng = random.Random(3)
    for _ in range(30):  # random networks with capacity-1 bottlenecks, feasible or not
        net = {}
        for _ in range(60):
            a, b = rng.randrange(15), rng.randrange(15)
            if a != b:
                net[(a, b)] = (rng.choice((1, 2, 2)), rng.randrange(0, 20))
        flow = _two_unit_min_cost_flow(net, 0, 14)
        assert cost(net, flow) == nx_cost(net, 0, 14)
        if flow is not None:  # conservation: two units leave the source, two reach the sink
            out0 = sum(f for (a, _), f in flow.items() if a == 0) - sum(f for (_, b), f in flow.items() if b == 0)
            assert out0 == 2 and all(f <= net[e][0] for e, f in flow.items())


def test_blocked_crossings_handles_one_way_bridges():
    """Dual-carriageway bridges are one-way corridors (found on the Scenario 1 map): explaining a single point
    of failure lists the blocked crossings without assuming that the corridor exists in its forward direction."""
    from coa_net.explain import _blocked_crossings
    from coa_net.mission import build_context
    from coa_net.parsing import parse_order
    from coa_net.pipeline import COANet
    from coa_net.scenarios import sample_text, synthetic_theatre

    G = synthetic_theatre(0).G
    pairs = sorted({tuple(sorted((u, v), key=str)) for u, v, d in G.edges(data=True) if d.get("bridge")}, key=str)
    for i, (u, v) in enumerate(pairs):  # every bridge one-way, alternating directions
        a, b = (u, v) if i % 2 else (v, u)
        if G.has_edge(a, b) and G.has_edge(b, a):
            G.remove_edge(a, b)
    s = COANet.from_graph(G, sim_reps=0, explain=False, recce=False)
    H = s.H
    oneway = [H.corridors[k] for eid, el in H.elements.items() if el.kind == "bridge" for k in H.element_corridors(eid)]
    assert any(c.fwd and not c.bwd for c in oneway) and any(c.bwd and not c.fwd for c in oneway)
    ctx = build_context(H, parse_order(sample_text("sample_order.txt"), H.G, s.catalogue), s.requirements, s.catalogue)
    for g in ctx.groups.values():
        for line in _blocked_crossings(H, g, ""):
            assert ": " in line and "(" in line


def test_route_text_counts_repeated_names():
    """BD TOPO names a bridge after its road: Scenario 1's routes read "via N166, N166, N166, ..." (one per bridge)."""
    from coa_net.candidates import compact_names

    assert compact_names(["D5", "D5", "N166", "N166", "N166", "D766", "D5"]) == "D5 x2, N166 x3, D766, D5"
    assert compact_names(["Pont-Est"]) == "Pont-Est" and compact_names([]) == ""

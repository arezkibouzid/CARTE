"""S5: robust network design, solved epsilon-lexicographically (methodology 5, S5; Appendix B.1).

A path-based model chooses for each group g one main route (MSR, x_gP) and
zero or more alternates (ASR, z_gP) among its candidates, designates the
corridors they use (y_a), and evaluates every loss scenario omega through
recourse variables r_gwP (the designated route g drives in omega) and
phi_gw (no designated route left). The two constraints that carry the design:

    survivability (NET.SURVIVE)  sum_{P: c not in C(P)} (x_gP + z_gP) >= 1 - u_gc
    detour guard (RTE.DETOUR)    T(P) z_gP <= rho T*_g     (T*_g: the group's fastest feasible time)

u_gc = 1 accepts c as a single point of failure: fixed to 1 for irreducible
elements, to 0 for the others when NET.SURVIVE is T1.

Objectives are never summed with weights. They are optimised one at a time in
a priority order, each level constrained by the optima already found:

    f_k* = min { f_k(x) : x feasible, f_j(x) <= f_j* + eps_j |f_j*| + eta_j for all j < k }

Deadlines are a guard (level 0, route-based lateness) whatever the order.
The model is solved with OR-Tools CP-SAT on integer-scaled coefficients; the
greedy fallback (MSR = lexicographically best candidate, ASR = cheapest
element-disjoint route within the guard) is used when OR-Tools is missing or
finds no solution in time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .candidates import CandidateSet, Route
from .corridors import CorridorGraph
from .criticality import FAILURE_TIME_MIN, Criticality
from .graph import is_two_lane, path_edges
from .mission import Group

MENU = ["threat_exposure", "resilience", "doctrine", "hazmat_builtup", "builtup", "travel_time", "network_size", "cover", "preferences"]
OBJECTIVE_INFO = {
    "lateness": ("min", "weighted lateness against deadlines (route-based guard)"),
    "threat_exposure": ("veh.km-eq", "expected threat exposure over the loss scenarios"),
    "resilience": ("veh.min", "expected delay over the loss scenarios, against each group's best time without loss"),
    "doctrine": ("count", "T2 route and network requirements not met (crossings, halts, separation)"),
    "hazmat_builtup": ("veh.km", "dangerous goods in built-up areas"),
    "builtup": ("veh.km", "vehicles in built-up areas"),
    "travel_time": ("veh.min", "weighted travel time on the main routes"),
    "network_size": ("km", "designated road length"),
    "cover": ("veh.km", "vehicles on open ground by day"),
    "preferences": ("veh.km", "vehicles on roads a T3 preference advises against or with unknown attributes"),
    "stability": ("count", "designations changed (repair only; moving units count double)"),
}
ETA = {"lateness": 0.0, "threat_exposure": 0.1, "resilience": 1.0, "doctrine": 0.0, "hazmat_builtup": 0.1, "builtup": 0.1,
       "travel_time": 1.0, "network_size": 0.1, "cover": 0.1, "preferences": 0.1, "stability": 0.0}

# Time-like objectives get a 10 % tolerance by default ("accept up to 10 % more"); the others none.
DEFAULT_EPS = {"travel_time": 0.10, "resilience": 0.10}
ARCHETYPES: dict[str, tuple[list[str], dict[str, float], str]] = {
    "P": (["threat_exposure", "resilience", "doctrine", "hazmat_builtup", "travel_time", "builtup", "network_size", "cover", "preferences"],
          dict(DEFAULT_EPS), "protected: contested area (default)"),
    "R": (["resilience", "threat_exposure", "travel_time", "doctrine", "hazmat_builtup", "builtup", "network_size", "cover", "preferences"],
          {"travel_time": 0.10, "resilience": 0.0}, "resilient: likely strikes on crossings, long operation"),
    "F": (["travel_time", "threat_exposure", "resilience", "doctrine", "hazmat_builtup", "builtup", "network_size", "cover", "preferences"],
          {"travel_time": 0.0, "resilience": 0.10}, "fast: urgent delivery, low threat"),
    "E": (["network_size", "threat_exposure", "resilience", "doctrine", "hazmat_builtup", "travel_time", "builtup", "cover", "preferences"],
          dict(DEFAULT_EPS), "economical: few assets to reconnoitre, mark and control routes"),
}
DEFAULT_ORDER = ARCHETYPES["P"][0]
SCALE = 100
RES_SCALE = 1000  # extra integer scaling of the resilience coefficients (probability x weight x vehicles)
UNIT = {"resilience": SCALE * RES_SCALE}


@dataclass
class Design:
    name: str
    order: list[str]
    eps: dict[str, float]
    msr: dict[str, Route]
    asrs: dict[str, list[Route]]
    designated: set[str]
    accepted: dict[str, set[str]]  # single points of failure accepted beyond the irreducible ones (waivers, or T2)
    irreducible: dict[str, set[str]]
    recourse: dict[tuple[str, str], Route | None]
    objectives: dict[str, float] = field(default_factory=dict)
    levels: list[tuple[str, float]] = field(default_factory=list)
    solver: str = ""
    status: str = ""
    seconds: float = 0.0
    waivers: list[str] = field(default_factory=list)
    waived: set[tuple[str, str]] = field(default_factory=set)  # (group, element) survivability waivers
    unplanned: dict[str, str] = field(default_factory=dict)  # group -> reason
    merged: list[str] = field(default_factory=list)
    description: str = ""

    def designated_length_km(self, H: CorridorGraph) -> float:
        return sum(H.corridors[c].length_m for c in self.designated) / 1000.0

    def routes(self, gid: str) -> list[Route]:
        return ([self.msr[gid]] if gid in self.msr else []) + self.asrs.get(gid, [])

    def signature(self) -> tuple:
        return tuple(sorted((g, r.key, tuple(sorted(a.key for a in self.asrs.get(g, [])))) for g, r in self.msr.items()))


# ---------------------------------------------------------------------------
# Objective coefficients
# ---------------------------------------------------------------------------


def _shortfalls(r: Route) -> int:
    return sum(1 for v in r.violations if v[1] == "T2")


def route_terms(g: Group, r: Route, phase: str) -> dict[str, float]:
    """Objective contributions of a route used as the group's MSR."""
    lat = 0.0
    if g.deadline is not None:
        lat = g.weight * max(0.0, g.earliest + r.time_min - g.deadline)
    return {
        "lateness": lat,
        "doctrine": g.n * _shortfalls(r),
        "hazmat_builtup": g.n * r.builtup_m / 1000.0 if g.hazmat else 0.0,
        "builtup": g.n * r.builtup_m / 1000.0,
        "travel_time": g.weight * g.n * r.time_min,
        "cover": g.n * r.open_m / 1000.0 if phase == "day" else 0.0,
        "preferences": g.n * r.pref_m / 1000.0,
    }


def _narrow_corridors(H: CorridorGraph, width: float, margin: float) -> set[str]:
    out = set()
    G = H.G
    for cid, k in H.corridors.items():
        for u, v in path_edges(k.nodes):
            d = G[u][v] if G.has_edge(u, v) else G[v][u]
            if is_two_lane(d, width, margin) is False:
                out.add(cid)
                break
    return out


# ---------------------------------------------------------------------------
# Evaluation of a given design (used by the greedy fallback and for reporting)
# ---------------------------------------------------------------------------


def evaluate(H: CorridorGraph, groups: dict[str, Group], crit: Criticality, d: Design, phase: str,
             separate: tuple[set[str], bool] | None = None, previous: "Design | None" = None, moving: set[str] | None = None) -> dict[str, float]:
    f = {k: 0.0 for k in ["lateness", *MENU, "stability"]}
    for gid, msr in d.msr.items():
        g = groups[gid]
        for k, v in route_terms(g, msr, phase).items():
            f[k] += v
    f["network_size"] = d.designated_length_km(H)
    for s in crit.scenarios:
        for gid, msr in d.msr.items():
            g = groups[gid]
            wn = g.weight * g.n
            if s.id == "S0":
                f["threat_exposure"] += s.p * wn * msr.exposure
                continue
            if not (s.lost & msr.elements):
                t, x = msr.time_min, msr.exposure
            else:
                r = d.recourse.get((gid, s.id))
                if r is not None and not (s.lost & r.elements):
                    t, x = r.time_min, r.exposure
                else:
                    t, x, _ = crit.fallback.get((gid, s.id), (FAILURE_TIME_MIN, 0.0, None))
            f["threat_exposure"] += s.p * wn * x
            f["resilience"] += s.p * wn * max(0.0, t - crit.best_time.get((gid, "S0"), t))
    if separate:
        narrow, _t1 = separate
        f["doctrine"] += _separation_conflicts(d, narrow)
    if previous is not None:
        f["stability"] = stability_count(d, previous, moving or set())
    return f


def _separation_conflicts(d: Design, narrow: set[str]) -> int:
    use: dict[str, set[tuple[str, int]]] = {}
    for gid, r in d.msr.items():
        for cid, dirn in r.arcs:
            if cid in narrow:
                use.setdefault(cid, set()).add((gid, dirn))
    n = 0
    for cid, s in use.items():
        dirs = {dirn for _, dirn in s}
        groups_f = {g for g, dd in s if dd > 0}
        groups_b = {g for g, dd in s if dd < 0}
        if len(dirs) == 2 and (groups_f - groups_b or groups_b - groups_f):
            n += 1
    return n


def stability_count(d: Design, prev: Design, moving: set[str]) -> int:
    n = 0
    for gid, r in d.msr.items():
        w = 2 if gid in moving else 1
        old = prev.msr.get(gid)
        if old is not None and old.key != r.key:
            n += w
        old_asr = {a.key for a in prev.asrs.get(gid, [])}
        new_asr = {a.key for a in d.asrs.get(gid, [])}
        n += len(old_asr ^ new_asr)
    return n


def _lex_key(vec: dict[str, float], order: list[str]) -> tuple:
    return tuple(round(vec.get(k, 0.0), 6) for k in order)


# ---------------------------------------------------------------------------
# CP-SAT model
# ---------------------------------------------------------------------------


@dataclass
class DesignProblem:
    H: CorridorGraph
    groups: dict[str, Group]
    cands: dict[str, CandidateSet]
    crit: Criticality
    phase: str = "day"
    fixed_msr: dict[str, Route] = field(default_factory=dict)  # repair: keep these MSRs
    keep_asr: dict[str, list[Route]] = field(default_factory=dict)  # repair: existing ASRs that may stay
    previous: Design | None = None
    moving: set[str] = field(default_factory=set)


def solve(prob: DesignProblem, order: list[str], eps: dict[str, float] | None = None, name: str = "I",
          time_limit_s: float = 10.0, workers: int = 1, use_cpsat: bool = True, description: str = "") -> Design:
    eps = dict(eps or {})
    t0 = time.perf_counter()
    if use_cpsat:
        try:
            d = _solve_cpsat(prob, order, eps, name, time_limit_s, workers)
            if d is not None:
                d.seconds = time.perf_counter() - t0
                d.description = description
                return d
        except ImportError:
            pass
    d = greedy(prob, order, eps, name)
    d.seconds = time.perf_counter() - t0
    d.description = description
    return d


def _survive_tier(g: Group) -> str | None:
    rs = g.active.of("NET.SURVIVE")
    if not rs:
        return None
    return "T1" if any(r.tier == "T1" for r in rs) else "T2"


def _beyond_guard(crit: Criticality, cs: CandidateSet, routes: list[Route], g: str) -> list[int]:
    """Candidates that may not be alternates: slower than rho times the group's best feasible time.

    The reference is T*_g, the fastest feasible route, which is also the reference S4 uses to call an
    element irreducible. Against the chosen MSR's own time (Appendix B.1 as first written) a design could
    pick a slower MSR only to admit an alternate."""
    rho = crit.rho.get(g, float("inf"))
    if rho == float("inf"):
        return []
    return [i for i, r in enumerate(routes) if r.time_min > rho * cs.best_time + 1e-6]


def _build(prob: DesignProblem, G_ids: list[str], P: dict[str, list[Route]], waived: set[tuple[str, str]]):
    """CP-SAT model of Appendix B.1 (constraints B1-B8 and the objective expressions)."""
    from ortools.sat.python import cp_model

    H, groups, cands, crit = prob.H, prob.groups, prob.cands, prob.crit
    m = cp_model.CpModel()
    x = {(g, i): m.NewBoolVar(f"x_{g}_{i}") for g in G_ids for i in range(len(P[g]))}
    z = {(g, i): m.NewBoolVar(f"z_{g}_{i}") for g in G_ids for i in range(len(P[g]))}
    corridors = sorted({c for g in G_ids for r in P[g] for c in r.corridors}, key=lambda c: int(c[1:]))
    y = {c: m.NewBoolVar(f"y_{c}") for c in corridors}
    for g in G_ids:
        n = len(P[g])
        m.AddExactlyOne(x[g, i] for i in range(n))  # B1
        kmax = groups[g].active.param("NET.SURVIVE", "max_alternates", 2)
        for i in range(n):
            m.Add(x[g, i] + z[g, i] <= 1)  # B2
        m.Add(sum(z[g, i] for i in range(n)) <= int(kmax))
        for i, r in enumerate(P[g]):
            for c in r.corridors:
                m.Add(x[g, i] + z[g, i] <= y[c])  # B3
        for i in _beyond_guard(crit, cands[g], P[g], g):  # B5, detour guard
            m.Add(z[g, i] == 0)
        if g in prob.fixed_msr:
            idx = [i for i, r in enumerate(P[g]) if r.key == prob.fixed_msr[g].key]
            if idx:
                m.Add(x[g, idx[0]] == 1)
    u = {}
    for g in G_ids:  # B4, survivability
        tier = _survive_tier(groups[g])
        kinds = set(groups[g].active.param("NET.SURVIVE", "element_kinds", []) or [])
        for c in sorted(cands[g].elements):
            if kinds and H.elements[c].kind not in kinds:
                continue
            uv = m.NewBoolVar(f"u_{g}_{c}")
            u[g, c] = uv
            m.Add(sum(x[g, i] + z[g, i] for i, r in enumerate(P[g]) if c not in r.elements) >= 1 - uv)
            if c in crit.irreducible.get(g, set()) or tier is None or (g, c) in waived:
                m.Add(uv == 1)
            elif tier == "T1":
                m.Add(uv == 0)
    caps = [r.p("max_km") for g in G_ids for r in groups[g].active.of("NET.SIZE", ("T1",)) if r.params.get("max_km")]
    if caps:  # B8
        m.Add(sum(int(round(H.corridors[c].length_m)) * y[c] for c in corridors) <= int(min(caps) * 1000))

    lin: dict[str, list] = {k: [] for k in ["lateness", *MENU, "stability"]}
    const: dict[str, int] = {k: 0 for k in lin}
    sep_reqs = [r for g in G_ids for r in groups[g].active.of("NET.SEPARATE")]
    narrow: set[str] = set()
    if sep_reqs:  # NET.SEPARATE: opposite flows of different groups on a narrow corridor
        width = max(groups[g].profile.width_m for g in G_ids)
        narrow = _narrow_corridors(H, width, sep_reqs[0].p("narrow_margin_m"))
        sep_t1 = any(r.tier == "T1" for r in sep_reqs)
        for c in sorted(narrow & set(corridors), key=lambda c: int(c[1:])):
            fwd = [(g, i) for g in G_ids for i, r in enumerate(P[g]) if (c, 1) in r.arcs]
            bwd = [(g, i) for g in G_ids for i, r in enumerate(P[g]) if (c, -1) in r.arcs]
            pairs = [(a, b) for a in fwd for b in bwd if a[0] != b[0]]
            if not pairs:
                continue
            v = m.NewBoolVar(f"sep_{c}")
            for (g1, i1), (g2, i2) in pairs:
                m.Add(v >= x[g1, i1] + x[g2, i2] - 1)
            if sep_t1:
                m.Add(v == 0)
            lin["doctrine"].append((SCALE, v))
    for g in G_ids:
        for i, r in enumerate(P[g]):
            for k, val in route_terms(groups[g], r, prob.phase).items():
                if val:
                    lin[k].append((int(round(val * SCALE)), x[g, i]))
    for c in corridors:
        lin["network_size"].append((int(round(H.corridors[c].length_m / 1000.0 * SCALE)), y[c]))
    rec: dict[tuple[str, str], tuple[dict[int, Any], Any]] = {}
    for s in crit.scenarios:  # B6, recourse per scenario
        for g in G_ids:
            wn = groups[g].weight * groups[g].n
            t_star = cands[g].best_time  # T*_g, the group's best time without any loss
            if s.id == "S0":
                for i, r in enumerate(P[g]):
                    if r.exposure:
                        lin["threat_exposure"].append((int(round(s.p * wn * r.exposure * SCALE)), x[g, i]))
                continue
            # resilience: delay in the scenario against T*_g (a constant), so a slower MSR is never rewarded
            if not any(s.lost & r.elements for r in P[g]):
                for i, r in enumerate(P[g]):
                    if r.exposure:
                        lin["threat_exposure"].append((int(round(s.p * wn * r.exposure * SCALE)), x[g, i]))
                    c = int(round(s.p * wn * RES_SCALE * max(0.0, r.time_min - t_star) * SCALE))
                    if c:
                        lin["resilience"].append((c, x[g, i]))
                continue
            surv = [i for i, r in enumerate(P[g]) if not (s.lost & r.elements)]
            rv = {i: m.NewBoolVar(f"r_{g}_{s.id}_{i}") for i in surv}
            phi = m.NewBoolVar(f"phi_{g}_{s.id}")
            rec[g, s.id] = (rv, phi)
            m.Add(sum(rv.values()) + phi == 1)
            for i in surv:
                m.Add(rv[i] <= x[g, i] + z[g, i])
                m.Add(rv[i] >= x[g, i])
                m.Add(phi <= 1 - x[g, i] - z[g, i])
            t_fb, x_fb, _ = crit.fallback.get((g, s.id), (FAILURE_TIME_MIN, 0.0, None))
            for i in surv:
                r = P[g][i]
                if r.exposure:
                    lin["threat_exposure"].append((int(round(s.p * wn * r.exposure * SCALE)), rv[i]))
                c = int(round(s.p * wn * RES_SCALE * max(0.0, r.time_min - t_star) * SCALE))
                if c:
                    lin["resilience"].append((c, rv[i]))
            lin["threat_exposure"].append((int(round(s.p * wn * x_fb * SCALE)), phi))
            lin["resilience"].append((int(round(s.p * wn * RES_SCALE * max(0.0, t_fb - t_star) * SCALE)), phi))
    if prob.previous is not None:  # stability: designations changed, moving units count double
        for g in G_ids:
            w = 2 if g in prob.moving else 1
            old = prob.previous.msr.get(g)
            if old is not None:
                const["stability"] += w * SCALE
                same = [i for i, r in enumerate(P[g]) if r.key == old.key]
                if same:
                    lin["stability"].append((-w * SCALE, x[g, same[0]]))
            old_asr = {a.key for a in prob.previous.asrs.get(g, [])}
            for i, r in enumerate(P[g]):
                if r.key in old_asr:
                    const["stability"] += SCALE
                    lin["stability"].append((-SCALE, z[g, i]))
                else:
                    lin["stability"].append((SCALE, z[g, i]))
    exprs = {k: (sum(c * v for c, v in lin[k]) + const[k]) if lin[k] else None for k in lin}
    return m, x, z, y, u, rec, exprs, corridors, narrow, sep_reqs, const


def _solve_cpsat(prob: DesignProblem, order: list[str], eps: dict[str, float], name: str, time_limit_s: float, workers: int) -> Design | None:
    from ortools.sat.python import cp_model

    H, groups, cands, crit = prob.H, prob.groups, prob.cands, prob.crit
    unplanned = {g: "; ".join(cands[g].notes) or "no feasible route" for g in groups if not cands[g].routes}
    G_ids = [g for g in groups if cands[g].routes]
    P = {g: cands[g].routes for g in G_ids}
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = 0
    waivers: list[str] = []
    waived: set[tuple[str, str]] = set()
    built = _build(prob, G_ids, P, waived)
    status = solver.Solve(built[0])
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        waived = _waiver_model(prob, G_ids, P) or set()
        if not waived:
            return None
        for g, c in sorted(waived):
            waivers.append(f"NET.SURVIVE waiver proposed for {g} on {H.elements[c].name} ({c}): no designated set within the limits avoids it")
        built = _build(prob, G_ids, P, waived)
        status = solver.Solve(built[0])
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
    m, x, z, y, u, rec, exprs, corridors, narrow, sep_reqs, const = built
    levels_order = ["lateness"] + [k for k in order if k != "lateness"]
    if prob.previous is not None and "stability" not in levels_order:
        levels_order.append("stability")
    levels: list[tuple[str, float]] = []
    exact = True
    for k in levels_order:
        e = exprs.get(k)
        if e is None:
            levels.append((k, const.get(k, 0) / UNIT.get(k, SCALE)))
            continue
        _hint(m, solver, list(x.values()) + list(z.values()) + list(y.values()))
        m.Minimize(e)
        st = solver.Solve(m)
        if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            exact = False
            break
        exact = exact and st == cp_model.OPTIMAL
        best = int(round(solver.ObjectiveValue()))
        unit = UNIT.get(k, SCALE)
        tol = int(round(eps.get(k, 0.0) * abs(best) + ETA.get(k, 0.0) * unit))
        m.Add(e <= best + tol)
        if k == "travel_time" and eps.get(k, 0.0) > 0:
            # the tolerance also holds per group, so that it is not spent on a single convoy
            for g in G_ids:
                tg = [int(round(r.time_min * SCALE)) for r in P[g]]
                now = sum(t for i, t in enumerate(tg) if solver.Value(x[g, i]))
                cap = max(int(round((1 + eps[k]) * cands[g].best_time * SCALE)), now)
                m.Add(sum(t * x[g, i] for i, t in enumerate(tg)) <= cap)
        levels.append((k, best / unit))
    # deterministic tie-break: earlier (faster) candidates, fewest alternates
    _hint(m, solver, list(x.values()) + list(z.values()) + list(y.values()))
    m.Minimize(sum(i * x[g, i] + (i + 1) * z[g, i] for g in G_ids for i in range(len(P[g]))))
    st = solver.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    msr = {g: P[g][next(i for i in range(len(P[g])) if solver.Value(x[g, i]))] for g in G_ids}
    asrs = {g: [P[g][i] for i in range(len(P[g])) if solver.Value(z[g, i])] for g in G_ids}
    designated = {c for g in G_ids for r in [msr[g]] + asrs[g] for c in r.corridors}
    accepted = {g: {c for c in msr[g].elements if c not in crit.irreducible.get(g, set()) and all(c in a.elements for a in asrs[g])}
                for g in G_ids}
    recourse: dict[tuple[str, str], Route | None] = {}
    for s in crit.scenarios:
        for g in G_ids:
            if (g, s.id) in rec:
                rv, phi = rec[g, s.id]
                pick = [i for i, v in rv.items() if solver.Value(v)]
                recourse[g, s.id] = P[g][pick[0]] if pick else None
            else:
                recourse[g, s.id] = msr[g]
    d = Design(
        name=name,
        order=levels_order,
        eps=eps,
        msr=msr,
        asrs=asrs,
        designated=designated,
        accepted=accepted,
        irreducible={g: set(crit.irreducible.get(g, set())) for g in G_ids},
        recourse=recourse,
        levels=levels,
        solver="cp-sat",
        status="optimal" if exact else "feasible (time limit)",
        waivers=waivers,
        waived=set(waived),
        unplanned=unplanned,
    )
    narrow_info = (narrow, any(r.tier == "T1" for r in sep_reqs)) if sep_reqs else None
    d.objectives = evaluate(H, groups, crit, d, prob.phase, narrow_info, prob.previous, prob.moving)
    return d


def _hint(m, solver, variables) -> None:
    m.ClearHints()
    try:
        for v in variables:
            m.AddHint(v, solver.Value(v))
    except Exception:  # noqa: BLE001 - no previous solution
        m.ClearHints()


def _waiver_model(prob: DesignProblem, G_ids: list[str], P: dict[str, list[Route]]) -> set[tuple[str, str]] | None:
    """Smallest set of (group, element) survivability waivers that restores feasibility."""
    from ortools.sat.python import cp_model

    groups, cands, crit = prob.groups, prob.cands, prob.crit
    m = cp_model.CpModel()
    x = {(g, i): m.NewBoolVar("") for g in G_ids for i in range(len(P[g]))}
    z = {(g, i): m.NewBoolVar("") for g in G_ids for i in range(len(P[g]))}
    w = {}
    for g in G_ids:
        n = len(P[g])
        m.AddExactlyOne(x[g, i] for i in range(n))
        kmax = groups[g].active.param("NET.SURVIVE", "max_alternates", 2)
        for i in range(n):
            m.Add(x[g, i] + z[g, i] <= 1)
        m.Add(sum(z[g, i] for i in range(n)) <= int(kmax))
        for i in _beyond_guard(crit, cands[g], P[g], g):
            m.Add(z[g, i] == 0)
        if g in prob.fixed_msr:
            idx = [i for i, r in enumerate(P[g]) if r.key == prob.fixed_msr[g].key]
            if idx:
                m.Add(x[g, idx[0]] == 1)
        if _survive_tier(groups[g]) != "T1":
            continue
        for c in sorted(cands[g].elements):
            if c in crit.irreducible.get(g, set()):
                continue
            wv = m.NewBoolVar("")
            w[g, c] = wv
            m.Add(sum(x[g, i] + z[g, i] for i, r in enumerate(P[g]) if c not in r.elements) >= 1 - wv)
    m.Minimize(sum(w.values()))
    s = cp_model.CpSolver()
    s.parameters.max_time_in_seconds = 10
    s.parameters.num_workers = 1
    st = s.Solve(m)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return {k for k, v in w.items() if s.Value(v)}


def _recourse(crit: Criticality, msr: dict[str, Route], asrs: dict[str, list[Route]], order: list[str]) -> dict[tuple[str, str], Route | None]:
    """Recourse of a heuristic design: the surviving designated route best on the first of exposure and resilience in the order."""
    prefer_exposure = "threat_exposure" in order and ("resilience" not in order or order.index("threat_exposure") < order.index("resilience"))
    out: dict[tuple[str, str], Route | None] = {}
    for s in crit.scenarios:
        for g, r in msr.items():
            if not (s.lost & r.elements):
                out[(g, s.id)] = r
                continue
            surv = [a for a in asrs.get(g, []) if not (s.lost & a.elements)]
            if not surv:
                out[(g, s.id)] = None
            elif prefer_exposure:
                out[(g, s.id)] = min(surv, key=lambda a: (a.exposure, a.time_min))
            else:
                out[(g, s.id)] = min(surv, key=lambda a: (a.time_min, a.exposure))
    return out


# ---------------------------------------------------------------------------
# Greedy fallback (= baseline B3 generalised; methodology 8.3)
# ---------------------------------------------------------------------------


def greedy(prob: DesignProblem, order: list[str], eps: dict[str, float] | None = None, name: str = "I") -> Design:
    H, groups, cands, crit = prob.H, prob.groups, prob.cands, prob.crit
    levels_order = ["lateness"] + [k for k in order if k not in ("lateness", "resilience", "network_size")]
    msr: dict[str, Route] = {}
    asrs: dict[str, list[Route]] = {}
    unplanned = {}
    for gid, g in groups.items():
        cs = cands[gid]
        if not cs.routes:
            unplanned[gid] = "; ".join(cs.notes) or "no feasible route"
            continue
        if gid in prob.fixed_msr:
            msr[gid] = prob.fixed_msr[gid]
        else:
            msr[gid] = min(cs.routes, key=lambda r: _lex_key(route_terms(g, r, prob.phase) | {"threat_exposure": g.weight * g.n * r.exposure}, levels_order))
        rho = crit.rho.get(gid, float("inf"))
        within = [r for r in cs.routes if r.key != msr[gid].key and r.time_min <= rho * cs.best_time + 1e-6]
        need = {c for c in msr[gid].elements if c not in crit.irreducible.get(gid, set())}
        chosen: list[Route] = []
        kmax = int(g.active.param("NET.SURVIVE", "max_alternates", 2))
        disjoint = [r for r in within if not (r.elements & need)]
        if need and disjoint:
            chosen.append(min(disjoint, key=lambda r: (r.time_min, r.exposure)))
        else:
            for c in sorted(need, key=str):
                if any(c not in a.elements for a in chosen) or len(chosen) >= kmax:
                    continue
                opts = [r for r in within if c not in r.elements]
                if opts:
                    chosen.append(min(opts, key=lambda r: (r.time_min, r.exposure)))
        asrs[gid] = chosen
    designated = {c for g in msr for r in [msr[g]] + asrs[g] for c in r.corridors}
    accepted = {}
    for g in msr:
        accepted[g] = {c for c in msr[g].elements if c not in crit.irreducible.get(g, set()) and all(c in a.elements for a in asrs[g])}
    d = Design(
        name=name,
        order=["lateness"] + [k for k in order if k != "lateness"],
        eps=dict(eps or {}),
        msr=msr,
        asrs=asrs,
        designated=designated,
        accepted=accepted,
        irreducible={g: set(crit.irreducible.get(g, set())) for g in msr},
        recourse=_recourse(crit, msr, asrs, order),
        solver="greedy",
        status="heuristic",
        unplanned=unplanned,
    )
    d.objectives = evaluate(H, groups, crit, d, prob.phase, None, prob.previous, prob.moving)
    return d


# ---------------------------------------------------------------------------
# Courses of action
# ---------------------------------------------------------------------------


def make_coas(prob: DesignProblem, intent_order: list[str] | None = None, intent_eps: dict[str, float] | None = None,
              archetypes: list[str] | None = None, merge_share: float = 0.03, **kw) -> tuple[list[Design], str]:
    """One design per archetype order plus the intent order; near-identical networks are merged.

    Returns the COAs and the name of the default COA (the one built from the intent order)."""
    names = archetypes or list(ARCHETYPES)
    designs: list[Design] = []
    if intent_order:
        designs.append(solve(prob, intent_order, intent_eps or dict(DEFAULT_EPS), "I", description="intent: order confirmed from the commander's intent", **kw))
    for nm in names:
        order, eps, desc = ARCHETYPES[nm]
        designs.append(solve(prob, order, eps, nm, description=desc, **kw))
    merged: list[Design] = []
    for d in designs:
        twin = next((o for o in merged if _same_network(prob.H, o, d, merge_share)), None)
        if twin is None:
            merged.append(d)
        else:
            twin.merged.append(d.name)
    default = merged[0].name
    return merged, default


def _same_network(H: CorridorGraph, a: Design, b: Design, share: float) -> bool:
    if a.signature() == b.signature():
        return True
    if {g: r.key for g, r in a.msr.items()} != {g: r.key for g, r in b.msr.items()}:
        return False
    diff = sum(H.corridors[c].length_m for c in a.designated ^ b.designated)
    total = max(sum(H.corridors[c].length_m for c in a.designated | b.designated), 1.0)
    return diff / total < share


def coa_label(d: Design) -> str:
    return "=".join([d.name, *d.merged])


__all__ = [
    "ARCHETYPES",
    "DEFAULT_EPS",
    "DEFAULT_ORDER",
    "Design",
    "DesignProblem",
    "MENU",
    "OBJECTIVE_INFO",
    "coa_label",
    "evaluate",
    "greedy",
    "make_coas",
    "route_terms",
    "solve",
    "stability_count",
]

"""Contrastive explanations (methodology 4.6, role L8).

For a decision, code finds the best plan that differs on it and compares the
two in tier and priority order:

1. a T0 or T1 violation of the alternative is the reason;
2. otherwise the first objective, in the confirmed order, on which the
   alternative is worse beyond its tolerance.

Decisions explained here: the main route of each group (against the fastest
road route with no doctrine at all, and against the best design that does not
use that route), each irreducible single point of failure (against the best
route that avoids the element), and each hold or wait of the schedule (against
leaving at the earliest time). The comparison uses the optimiser's own vector,
so the reason is exact by construction. A language model may phrase a record
(role L8); every number and identifier of its sentence must appear in the
record, otherwise the template sentence stays.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from .candidates import CandidateSet, Route, allowed_halt_nodes, hard_violations, make_route, search
from .corridors import CorridorGraph
from .costing import GroupCoster
from .criticality import Criticality
from .design import DEFAULT_EPS, ETA, OBJECTIVE_INFO, Design, DesignProblem, _recourse, evaluate, solve
from .graph import ukey
from .llm import call_validated, unsupported
from .mission import Group, MissionContext
from .refine import Refinement
from .requirements import ActiveSet
from .schedule import Schedule, can_meet, headway_min, hhmm

TIER_RANK = {"T0": 0, "T1": 1, "T2": 2, "T3": 3}


@dataclass
class Contrast:
    subject: str  # "C1 MSR LION"
    alternative: str  # "fastest road route, 14.2 km 17.8 min via Pont-Centre"
    kind: str  # rule | survivability | objective | same | none
    reason: str
    sentence: str
    values: dict[str, tuple[float, float]] = field(default_factory=dict)  # objective -> (chosen, alternative)
    source: str = "template"

    def facts(self) -> str:
        vals = "; ".join(f"{k}: chosen {a:.1f}, alternative {b:.1f} {OBJECTIVE_INFO[k][0]}" for k, (a, b) in self.values.items())
        return "\n".join(x for x in (self.subject, self.alternative, self.reason, self.sentence, vals) if x)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _where(H: CorridorGraph, u: Any, v: Any) -> str:
    eid = H.edge_elements.get(ukey(u, v))
    if eid is not None:
        return H.elements[eid].name
    return f"{H.label(u)}-{H.label(v)}"


def _route_name(ref: Refinement | None, r: Route, role: str) -> str:
    nm = ref.name_of(r) if ref is not None else None
    return f"{role} {nm}" if nm else role


def _brief(H: CorridorGraph, r: Route) -> str:
    return r.describe(H)


def road_route(H: CorridorGraph, group: Group) -> Route | None:
    """Fastest road route with no doctrine at all (closures still apply): what a planner without rules would pick."""
    act = ActiveSet([q for q in group.active.reqs if q.template == "SEG.STATUS"])
    c0 = GroupCoster(H, group.profile, act, group.coster.phase, overrides=group.coster.overrides)
    pts = [group.origin, *group.via, group.destination]
    arcs: list = []
    for a, b in zip(pts[:-1], pts[1:]):
        p = search(H, c0, a, b)
        if p is None:
            return None
        arcs.extend(p)
    return make_route(H, group, tuple(arcs), set(), allowed_halt_nodes(H, group), {"road"})


def blocks(H: CorridorGraph, group: Group, r: Route) -> list[tuple[str, str, str]]:
    """T0/T1 violations of a route for a group, in tier then driving order: (requirement, tier, "where: detail")."""
    out: list[tuple[int, int, str, str, str]] = []
    for i, (u, v) in enumerate(zip(r.nodes[:-1], r.nodes[1:])):
        for rid, tier, detail in group.coster.evaluate(u, v).blocked:
            out.append((TIER_RANK.get(tier, 9), i, rid, tier, f"{_where(H, u, v)}: {detail}"))
    for rid, tier, detail in hard_violations(r):
        out.append((TIER_RANK.get(tier, 9), len(r.nodes), rid, tier, detail))
    seen, res = set(), []
    for _, _, rid, tier, what in sorted(out):
        if (rid, what) not in seen:
            seen.add((rid, what))
            res.append((rid, tier, what))
    return res


def _template(rid: str, group: Group) -> str:
    q = next((q for q in group.active.reqs if q.id == rid), None)
    return q.template if q is not None else ""


def _uncovered(design: Design, gid: str, msr: Route, crit: Criticality) -> list[str]:
    """Reducible elements of a main route that no designated alternate of the group avoids."""
    irr = set(crit.irreducible.get(gid, set())) | {e for g, e in design.waived if g == gid}
    alts = [a for a in design.asrs.get(gid, []) if a.key != msr.key]
    return sorted(e for e in msr.elements if e not in irr and all(e in a.elements for a in alts))


def compare(base: dict[str, float], alt: dict[str, float], order: list[str], eps: dict[str, float] | None = None) -> tuple[str | None, float, float]:
    """First objective, in order, on which ``alt`` differs from ``base`` beyond its tolerance: (objective, base, alt)."""
    eps = DEFAULT_EPS if eps is None else eps
    levels = ["lateness"] + [k for k in order if k != "lateness"]
    for k in levels:
        a, b = base.get(k, 0.0), alt.get(k, 0.0)
        tol = max(ETA.get(k, 0.1), eps.get(k, 0.0) * abs(a))
        if abs(b - a) > tol:
            return k, a, b
    return None, 0.0, 0.0


def _substitute(H: CorridorGraph, crit: Criticality, design: Design, gid: str, r: Route) -> Design:
    msr = {**design.msr, gid: r}
    asrs = {g: [a for a in rs if not (g == gid and a.key == r.key)] for g, rs in design.asrs.items()}
    designated = {c for g, m in msr.items() for x in [m, *asrs.get(g, [])] for c in x.corridors}
    return replace(design, name=design.name + "*", msr=msr, asrs=asrs, designated=designated,
                   recourse=_recourse(crit, msr, asrs, design.order), objectives={})


def _objective_sentence(k: str, a: float, b: float) -> str:
    unit = OBJECTIVE_INFO[k][0]
    what = "worse" if b > a else "better"
    return f"{k} {b:.1f} {unit} instead of {a:.1f} ({what})"


# ---------------------------------------------------------------------------
# Main routes
# ---------------------------------------------------------------------------


def explain_msr_vs_road(H: CorridorGraph, ctx: MissionContext, crit: Criticality, design: Design, gid: str,
                        ref: Refinement | None = None, phase: str = "day") -> Contrast:
    g = ctx.groups[gid]
    msr = design.msr[gid]
    subject = f"{gid} {_route_name(ref, msr, 'MSR')} ({_brief(H, msr)})"
    road = road_route(H, g)
    if road is None:
        return Contrast(subject, "no road route", "none", "no road route between start and destination", f"{gid}: no road route exists.")
    alt = f"fastest road route without doctrine ({_brief(H, road)})"
    if road.key == msr.key:
        return Contrast(subject, alt, "same", "", f"{gid} takes the fastest road route ({_brief(H, msr)}).")
    bad = blocks(H, g, road)
    if bad:
        rid, tier, what = bad[0]
        more = f" (and {len(bad) - 1} more)" if len(bad) > 1 else ""
        reason = f"{what} ({rid} {_template(rid, g)}, {tier})"
        return Contrast(subject, alt, "rule", reason,
                        f"{gid} does not take the fastest road route ({_brief(H, road)}): {reason}{more}.")
    unc = _uncovered(design, gid, road, crit)
    if unc and any(q.tier == "T1" for q in g.active.of("NET.SURVIVE")):
        names = ", ".join(H.elements[e].name for e in unc)
        reason = f"no designated alternate would avoid {names} (NET.SURVIVE, T1)"
        return Contrast(subject, alt, "survivability", reason, f"{gid} does not take the fastest road route ({_brief(H, road)}): {reason}.")
    d2 = _substitute(H, crit, design, gid, road)
    base = evaluate(H, ctx.groups, crit, design, phase)
    other = evaluate(H, ctx.groups, crit, d2, phase)
    k, a, b = compare(base, other, design.order, design.eps)
    if k is None:
        return Contrast(subject, alt, "same", "same values on every objective",
                        f"{gid}: the fastest road route ({_brief(H, road)}) gives the same values; the tie-break kept {_brief(H, msr)}.")
    rank = [x for x in ["lateness"] + [o for o in design.order if o != "lateness"]].index(k)
    reason = f"{_objective_sentence(k, a, b)}, priority {rank}"
    return Contrast(subject, alt, "objective", reason,
                    f"{gid} does not take the fastest road route ({_brief(H, road)}): {reason}.", {k: (a, b)})


def explain_msr_vs_next(H: CorridorGraph, ctx: MissionContext, prob: DesignProblem, design: Design, gid: str,
                        ref: Refinement | None = None, time_limit_s: float = 3.0, use_cpsat: bool = True) -> Contrast:
    """Re-solve the design with the group's main route removed from its pool and compare in priority order."""
    msr = design.msr[gid]
    subject = f"{gid} {_route_name(ref, msr, 'MSR')} ({_brief(H, msr)})"
    cs: CandidateSet = prob.cands[gid]
    pool = [r for r in cs.routes if r.key != msr.key]
    if not pool:
        return Contrast(subject, "no other candidate", "none", "the only feasible route", f"{gid}: {_brief(H, msr)} is the only feasible route.")
    cands = {**prob.cands, gid: replace(cs, routes=pool)}
    alt_prob = replace(prob, cands=cands)
    order = [k for k in design.order if k not in ("lateness", "stability")]
    alt = solve(alt_prob, order, design.eps, name=design.name + "'", time_limit_s=time_limit_s, use_cpsat=use_cpsat)
    if gid not in alt.msr:
        return Contrast(subject, "no plan without it", "none", "no design without this route", f"{gid}: no design exists without {_brief(H, msr)}.")
    r2 = alt.msr[gid]
    alt_text = f"best design without it, MSR {_brief(H, r2)}"
    k, a, b = compare(design.objectives, alt.objectives, design.order, design.eps)
    if k is None:
        return Contrast(subject, alt_text, "same", "same values on every objective",
                        f"{gid}: a design with MSR {_brief(H, r2)} gives the same values (within tolerance); the tie-break kept {_brief(H, msr)}.")
    levels = ["lateness"] + [o for o in design.order if o != "lateness"]
    reason = f"{_objective_sentence(k, a, b)}, priority {levels.index(k)}"
    others = {o: (design.objectives.get(o, 0.0), alt.objectives.get(o, 0.0)) for o in levels[: levels.index(k) + 1]}
    return Contrast(subject, alt_text, "objective", reason,
                    f"{gid} keeps {_brief(H, msr)} rather than {_brief(H, r2)}: the best design without it has {reason}.", others)


# ---------------------------------------------------------------------------
# Single points of failure
# ---------------------------------------------------------------------------


def _blocked_crossings(H: CorridorGraph, g: Group, skip: str) -> list[str]:
    """Bridges and tunnels the group may not use, with the reason."""
    out = []
    for eid, el in H.elements.items():
        if eid == skip or el.kind not in ("bridge", "tunnel"):
            continue
        k = next(iter(H.element_corridors(eid)), None)
        if k is None:
            continue
        for u, v in H.corridors[k].arc_edges(1):
            if H.edge_elements.get(ukey(u, v)) != eid:
                continue
            b = g.coster.evaluate(u, v).blocked
            if b:
                rid, tier, detail = b[0]
                out.append(f"{el.name}: {detail} ({rid}, {tier})")
            break
    return out


def explain_spof(H: CorridorGraph, ctx: MissionContext, cands: dict[str, CandidateSet], crit: Criticality, gid: str, eid: str) -> Contrast:
    g = ctx.groups[gid]
    el = H.elements[eid]
    cs = cands[gid]
    subject = f"{gid}: {el.name} is an irreducible single point of failure"
    if el.kind == "articulation":
        reason = f"every road from {H.label(g.origin)} to {H.label(g.destination)} passes through {el.name}"
        return Contrast(subject, "none", "rule", reason, f"{subject}: {reason}.")
    rep = cs.replacement.get(eid)
    rho = g.active.param("RTE.DETOUR", "rho", None)
    if rep is None:
        why = _blocked_crossings(H, g, eid)
        reason = "no usable road route avoids it" + (": " + "; ".join(why[:4]) if why else "")
        return Contrast(subject, "none", "rule", reason, f"{subject}: {reason}.")
    ratio = rep.time_min / max(cs.best_time, 1e-9)
    if rho is not None and ratio > rho + 1e-9:
        rid = next(iter(g.active.ids("RTE.DETOUR")), "RTE.DETOUR")
        reason = (f"the best route avoiding it ({_brief(H, rep)}) takes {ratio:.2f} times the best time "
                  f"{cs.best_time:.1f} min, more than the detour limit {rho:g} ({rid} RTE.DETOUR)")
        return Contrast(subject, f"best route avoiding {el.name}: {_brief(H, rep)}", "rule", reason, f"{subject}: {reason}.")
    bad = blocks(H, g, rep)
    if bad:
        rid, tier, what = bad[0]
        reason = f"the best route avoiding it ({_brief(H, rep)}) violates {what} ({rid}, {tier})"
        return Contrast(subject, f"best route avoiding {el.name}: {_brief(H, rep)}", "rule", reason, f"{subject}: {reason}.")
    reason = f"a route avoids it ({_brief(H, rep)}) but it was not kept in the candidate pool"
    return Contrast(subject, f"best route avoiding {el.name}: {_brief(H, rep)}", "none", reason, f"{subject}: {reason}.")


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def explain_holds(H: CorridorGraph, ctx: MissionContext, sched: Schedule, ref: Refinement | None = None, tol: float = 0.25) -> list[Contrast]:
    """Why a unit leaves later than its earliest time, or waits on the way: the binding constraint."""
    out = []
    credits = list(sched.credits.values())
    for c in credits:
        g = ctx.groups[c.group]
        stops = []
        if c.hold > tol:
            stops.append((0, c.hold, "holds"))
        for i, l in enumerate(c.legs[1:], start=1):
            if l.wait_before > tol:
                stops.append((i, l.wait_before, "waits"))
        for i, minutes, verb in stops:
            leg = c.legs[i]
            where = H.label(leg.start)
            reason = ""
            for j in range(i, len(c.legs)):
                lj = c.legs[j]
                for o in credits:
                    if o.unit == c.unit:
                        continue
                    for lo in o.legs:
                        if lo.arc[0] != lj.arc[0]:
                            continue
                        if lo.arc == lj.arc and lo.t_in <= lj.t_in:
                            h = headway_min(g)
                            if abs(lj.t_in - (lo.t_in + lo.pass_min + h)) <= tol or abs(lj.t_out - (lo.t_out + lo.pass_min + h)) <= tol:
                                rid = next(iter(g.active.ids("SCH.HEADWAY")), "SCH.HEADWAY")
                                reason = f"to keep {h:g} min behind {o.unit} on {_road(H, lj.arc)} ({rid} SCH.HEADWAY)"
                        elif lo.arc != lj.arc and not can_meet(H, lj.arc[0], g.profile.width_m, ctx.groups[o.group].profile.width_m):
                            if abs(lj.t_in - lo.tail_out) <= tol:
                                reason = f"to let {o.unit} clear the single-lane {_road(H, lj.arc)} coming the other way"
                        if reason:
                            break
                    if reason:
                        break
                if reason:
                    break
            if not reason:
                for q in g.active.of("SCH.WINDOW"):
                    end = float(q.p("end_min"))
                    if any(abs(l.t_in - end) <= tol for l in c.legs[i:]):
                        reason = f"until the built-up window {hhmm(float(q.p('start_min')))}-{hhmm(end)} closes ({q.id} SCH.WINDOW)"
                        break
            if not reason:
                reason = "joint schedule of the columns (no single binding constraint found)"
            text = f"{c.unit} {verb} {minutes:.0f} min at {where} {reason}."
            out.append(Contrast(f"{c.unit} {verb} {minutes:.0f} min at {where}", f"leave at {hhmm(c.earliest) if i == 0 else 'once'}",
                                "rule" if "(" in reason else "none", reason, text))
    return out


def _road(H: CorridorGraph, arc) -> str:
    names = [n for n in H.corridors[arc[0]].names if n]
    return names[0] if names else f"the road {H.label(H.arc_start(arc))}-{H.label(H.arc_end(arc))}"


# ---------------------------------------------------------------------------
# Whole plan and phrasing
# ---------------------------------------------------------------------------


def explain_plan(H: CorridorGraph, ctx: MissionContext, prob: DesignProblem, design: Design, sched: Schedule | None = None,
                 ref: Refinement | None = None, resolve: bool = True, llm=None, use_cpsat: bool = True) -> list[Contrast]:
    out: list[Contrast] = []
    for gid in sorted(design.msr):
        out.append(explain_msr_vs_road(H, ctx, prob.crit, design, gid, ref, prob.phase))
        if resolve:
            out.append(explain_msr_vs_next(H, ctx, prob, design, gid, ref, use_cpsat=use_cpsat))
    for gid in sorted(design.msr):
        for e in design.msr[gid].ordered_elements(H):
            if e in design.irreducible.get(gid, set()):
                out.append(explain_spof(H, ctx, prob.cands, prob.crit, gid, e))
    if sched is not None:
        out.extend(explain_holds(H, ctx, sched, ref))
    if llm is not None:
        out = [phrase(c, llm) for c in out]
    return out


L8_SYSTEM = (
    "You rephrase one explanation of a route planning decision for a military staff officer, in one or two short "
    "sentences. Keep every number, unit, place and requirement identifier exactly as given; add none."
)


def phrase(c: Contrast, llm) -> Contrast:
    schema = {"type": "object", "properties": {"sentence": {"type": "string"}}, "required": ["sentence"]}
    facts = c.facts()

    def validate(out):
        text = str(out.get("sentence", "")) if isinstance(out, dict) else ""
        problems = []
        if not text:
            problems.append("empty")
        bad = unsupported(text, facts)
        if bad:
            problems.append("not in the record: " + ", ".join(bad[:6]))
        return text, problems

    text, _ = call_validated(llm, L8_SYSTEM, "Record:\n" + facts, schema, "explanation", validate)
    return replace(c, sentence=text, source="model") if text else c


def explanations_markdown(cs: list[Contrast]) -> str:
    return "\n".join(f"- {c.sentence}" for c in cs if c.kind != "same" or "tie-break" not in c.sentence)


__all__ = ["Contrast", "blocks", "compare", "explain_holds", "explain_msr_vs_next", "explain_msr_vs_road", "explain_plan",
           "explain_spof", "explanations_markdown", "phrase", "road_route"]

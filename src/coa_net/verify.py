"""S8 (1): rule checker at five levels (methodology 5, S8).

Every active requirement instance is re-checked on the final plan, three-valued,
independently of how the plan was built:

- SEG and PRO on every road edge of every designated route (fresh evaluation
  against the current map and beliefs: violations, shortfalls of T2
  instances, unknown values priced as risk);
- RTE on every route (halts, turns, crossings, range, detour guard);
- NET on the network (survivability with irreducible, accepted and waived
  single points of failure, separation, lateral links, control posts, size cap);
- SCH on the movement credits (time gap at both ends of every shared segment,
  meetings on single-lane roads, junctions, driving time between halts, halt
  areas and capacities, windows, deadlines, march speed and unit size), and
  PRO.CRITPOINT on waits and halts.

T0 and T1 violations must be zero or covered by a waiver; T2 shortfalls are
reported. The plan is valid when no unwaived T0/T1 violation remains.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from .candidates import Route, route_violations
from .corridors import CorridorGraph
from .criticality import Criticality
from .design import Design, _separation_conflicts, _narrow_corridors
from .graph import node_xy, point_distance
from .mission import Group, MissionContext
from .refine import Refinement, chainage
from .requirements import TEMPLATES
from .schedule import JUNCTION_CLEARANCE_MIN, TICK, Schedule, can_meet, headway_min

TOL_MIN = 0.05  # half a scheduling tick


@dataclass
class Finding:
    level: str  # SEG | RTE | NET | SCH | PRO
    req: str  # requirement id, or ORDER for the order's deadlines
    tier: str
    status: str  # violation | shortfall | unknown | irreducible | waived
    subject: str
    detail: str


@dataclass
class VerificationReport:
    findings: list[Finding] = field(default_factory=list)
    checked: dict[str, int] = field(default_factory=dict)

    @property
    def violations(self) -> list[Finding]:
        return [f for f in self.findings if f.status == "violation" and f.tier in ("T0", "T1")]

    @property
    def ok(self) -> bool:
        return not self.violations

    def count(self, status: str, level: str | None = None) -> int:
        return sum(1 for f in self.findings if f.status == status and (level is None or f.level == level))

    def summary(self) -> str:
        parts = [f"{sum(self.checked.values())} checks"]
        for st in ("violation", "shortfall", "unknown", "irreducible", "waived"):
            n = self.count(st)
            if n:
                parts.append(f"{n} {st}{'s' if n > 1 and not st.endswith('e') else ''}")
        return ("valid: " if self.ok else "INVALID: ") + ", ".join(parts)

    def table(self) -> list[dict]:
        return [f.__dict__ for f in self.findings]


def _level(rid: str, groups: dict[str, Group]) -> tuple[str, str]:
    for g in groups.values():
        for r in g.active.reqs:
            if r.id == rid:
                return TEMPLATES[r.template].level, r.template
    return "SEG", ""


def verify(H: CorridorGraph, ctx: MissionContext, design: Design, ref: Refinement | None = None,
           sched: Schedule | None = None, crit: Criticality | None = None) -> VerificationReport:
    rep = VerificationReport()
    groups = ctx.groups
    _check_roads(H, groups, design, ref, rep)
    _check_routes(H, groups, design, ref, rep)
    _check_network(H, groups, design, ref, crit, rep)
    if sched is not None:
        _check_schedule(H, ctx, sched, rep)
    for gid, why in design.unplanned.items():
        rep.findings.append(Finding("RTE", "ORDER", "T0", "violation", gid, f"no route: {why}"))
    return rep


def _route_label(ref: Refinement | None, gid: str, r: Route, role: str) -> str:
    nm = ref.name_of(r) if ref is not None else None
    return f"{gid} {role}{' ' + nm if nm else ''}"


def _designated(design: Design) -> list[tuple[str, str, Route]]:
    out = []
    for gid, r in design.msr.items():
        out.append((gid, "MSR", r))
        out.extend((gid, "ASR", a) for a in design.asrs.get(gid, []))
    return out


def _check_roads(H: CorridorGraph, groups: dict[str, Group], design: Design, ref: Refinement | None, rep: VerificationReport) -> None:
    n = 0
    for gid, role, r in _designated(design):
        g = groups[gid]
        subject = _route_label(ref, gid, r, role)
        blocked: dict[tuple[str, str], list[str]] = {}
        unknown: dict[str, list[float]] = {}
        builtup_m = hazmat_m = weather_m = 0.0
        for u, v in zip(r.nodes[:-1], r.nodes[1:]):
            n += 1
            ev = g.coster.evaluate(u, v)
            for rid, tier, detail in ev.blocked:
                blocked.setdefault((rid, tier), []).append(f"{H.label(u)}-{H.label(v)}: {detail}")
            for rid, detail in ev.unknown:
                unknown.setdefault(f"{rid}|{detail}", []).append(ev.length_m)
            d = H.G[u][v]
            if d.get("built_up"):
                builtup_m += ev.length_m
                if g.hazmat:
                    hazmat_m += ev.length_m
            if d.get("weather", 0.0) >= 0.5:
                weather_m += ev.length_m
        for (rid, tier), items in blocked.items():
            lvl, _ = _level(rid, groups)
            status = "violation" if tier in ("T0", "T1") else "shortfall"
            rep.findings.append(Finding(lvl, rid, tier, status, subject, f"{len(items)} road segment(s): {items[0]}" + (" ..." if len(items) > 1 else "")))
        for key, lengths in unknown.items():
            rid, detail = key.split("|", 1)
            lvl, _ = _level(rid, groups)
            rep.findings.append(Finding(lvl, rid, "T0", "unknown", subject, f"{detail} on {len(lengths)} road segment(s), {sum(lengths) / 1000:.1f} km (priced as risk)"))
        for q in g.active.of("SEG.AVOID", ("T2",)):
            if q.p("attribute") == "built_up" and builtup_m > 0:
                rep.findings.append(Finding("SEG", q.id, "T2", "shortfall", subject, f"{builtup_m / 1000:.1f} km in built-up areas"))
        for q in g.active.of("SEG.CARGO", ("T2",)):
            if q.p("element") == "built_up" and hazmat_m > 0:
                rep.findings.append(Finding("SEG", q.id, "T2", "shortfall", subject, f"dangerous goods: {hazmat_m / 1000:.1f} km in built-up areas"))
        for q in g.active.of("SEG.SURFACE", ("T2",)):
            if weather_m > 0:
                rep.findings.append(Finding("SEG", q.id, "T2", "shortfall", subject, f"{weather_m / 1000:.1f} km of weather-degraded road"))
        for q in g.active.of("PRO.EXPOSURE", ("T2",)):
            if r.exposure > 1e-6:
                rep.findings.append(Finding("PRO", q.id, "T2", "shortfall", subject, f"threat exposure {r.exposure:.2f} km-eq (max threat {r.max_threat:.2f})"))
    rep.checked["SEG"] = rep.checked.get("SEG", 0) + n


def _check_routes(H: CorridorGraph, groups: dict[str, Group], design: Design, ref: Refinement | None, rep: VerificationReport) -> None:
    n = 0
    for gid, role, r in _designated(design):
        g = groups[gid]
        subject = _route_label(ref, gid, r, role)
        for rid, tier, detail in route_violations(r, g):
            n += 1
            rep.findings.append(Finding("RTE", rid, tier, "violation" if tier in ("T0", "T1") else "shortfall", subject, detail))
        n += len(g.active.reqs)
    for gid, msr in design.msr.items():
        g = groups[gid]
        for q in g.active.of("RTE.DETOUR"):
            for a in design.asrs.get(gid, []):
                n += 1
                if a.time_min > q.p("rho") * msr.time_min + 1e-6:
                    rep.findings.append(Finding("RTE", q.id, q.tier, "violation", _route_label(ref, gid, a, "ASR"),
                                                f"{a.time_min:.1f} min > {q.p('rho')} x MSR {msr.time_min:.1f} min"))
    rep.checked["RTE"] = rep.checked.get("RTE", 0) + n


def _check_network(H: CorridorGraph, groups: dict[str, Group], design: Design, ref: Refinement | None,
                   crit: Criticality | None, rep: VerificationReport) -> None:
    n = 0
    for gid, msr in design.msr.items():
        g = groups[gid]
        rs = g.active.of("NET.SURVIVE")
        if not rs:
            continue
        tier = "T1" if any(r.tier == "T1" for r in rs) else "T2"
        rid = next(r.id for r in rs if r.tier == tier)
        kinds = set(g.active.param("NET.SURVIVE", "element_kinds", []) or [])
        routes = [msr] + design.asrs.get(gid, [])
        for c in msr.ordered_elements(H):
            if kinds and H.elements[c].kind not in kinds:
                continue
            n += 1
            if any(c not in r.elements for r in routes):
                continue
            el = H.elements[c]
            what = f"loss of {el.name} ({c}) leaves no designated route"
            if c in design.irreducible.get(gid, set()):
                rep.findings.append(Finding("NET", rid, tier, "irreducible", gid, what + ": no route within the detour guard avoids it"))
            elif (gid, c) in design.waived:
                rep.findings.append(Finding("NET", rid, tier, "waived", gid, what + " (waiver proposed to the operator)"))
            else:
                rep.findings.append(Finding("NET", rid, tier, "violation" if tier == "T1" else "shortfall", gid, what))
    sep = [r for g in groups.values() for r in g.active.of("NET.SEPARATE")]
    if sep:
        n += 1
        width = max(g.profile.width_m for g in groups.values())
        k = _separation_conflicts(design, _narrow_corridors(H, width, sep[0].p("narrow_margin_m")))
        if k:
            tier = "T1" if any(r.tier == "T1" for r in sep) else "T2"
            rep.findings.append(Finding("NET", sep[0].id, tier, "violation" if tier == "T1" else "shortfall", "network",
                                        f"{k} narrow corridor(s) carry opposite flows of different groups"))
    size = [r for g in groups.values() for r in g.active.of("NET.SIZE", ("T1",)) if r.params.get("max_km")]
    if size:
        n += 1
        km = design.designated_length_km(H)
        cap = min(r.p("max_km") for r in size)
        if km > cap + 1e-6:
            rep.findings.append(Finding("NET", size[0].id, "T1", "violation", "network", f"{km:.1f} km designated > {cap} km"))
    control = [r for g in groups.values() for r in g.active.of("NET.CONTROL")]
    if control and ref is not None:
        q = control[0]
        posts = {p.node for p in ref.posts}
        for nm, nr in ref.routes.items():
            n += 1
            if q.p("at_elements"):
                for e in nr.route.ordered_elements(H):
                    el = H.elements[e]
                    if el.kind != "articulation" and not (set(el.nodes) & posts):
                        rep.findings.append(Finding("NET", q.id, q.tier, "shortfall", nm, f"no control post at {el.name} ({e})"))
            pos = chainage(H, nr.route)
            marks = sorted({0.0, nr.route.length_m} | {pos[p] for p in posts if p in pos})
            gap = max((b - a for a, b in zip(marks, marks[1:])), default=0.0)
            if gap > q.p("max_spacing_km") * 1000 + 1e-6:
                rep.findings.append(Finding("NET", q.id, q.tier, "shortfall", nm, f"{gap / 1000:.1f} km between control posts > {q.p('max_spacing_km')} km"))
    lateral = [r for g in groups.values() for r in g.active.of("NET.LATERAL")]
    if lateral:
        q = lateral[0]
        for gid, msr in design.msr.items():
            for a in design.asrs.get(gid, []):
                n += 1
                gap = _lateral_gap(H, msr, a, q.p("link_max_km") * 1000)
                if gap > q.p("max_spacing_km") * 1000:
                    rep.findings.append(Finding("NET", q.id, q.tier, "shortfall", _route_label(ref, gid, a, "ASR"),
                                                f"{gap / 1000:.1f} km of main route without a lateral link to this alternate (max {q.p('max_spacing_km')} km)"))
    rep.checked["NET"] = rep.checked.get("NET", 0) + n


def _lateral_gap(H: CorridorGraph, msr: Route, asr: Route, link_m: float) -> float:
    """Longest stretch of the MSR with no node within ``link_m`` of the alternate (straight line)."""
    G = H.G
    pos = chainage(H, msr)
    a_xy = [node_xy(G, n) for n in asr.nodes]
    linked = [pos[n] for n in msr.nodes if any(point_distance(G, node_xy(G, n), p) <= link_m for p in a_xy)]
    marks = sorted(set(linked) | {0.0, msr.length_m}) if linked else [0.0, msr.length_m]
    return max(b - a for a, b in zip(marks, marks[1:])) if len(marks) > 1 else 0.0


def _check_schedule(H: CorridorGraph, ctx: MissionContext, sched: Schedule, rep: VerificationReport) -> None:
    G = H.G
    n = 0
    credits = list(sched.credits.values())
    groups = ctx.groups

    def req(c, template):
        return groups[c.group].active.of(template)

    for a, b in itertools.combinations(credits, 2):  # time gap and meetings
        ga, gb = groups[a.group], groups[b.group]
        h = max(headway_min(ga), headway_min(gb))
        rid = next((r.id for r in req(a, "SCH.HEADWAY") + req(b, "SCH.HEADWAY")), "SCH.HEADWAY")
        for la in a.legs:
            for lb in b.legs:
                if la.arc == lb.arc:
                    n += 1
                    lead, foll = (la, lb) if la.t_in <= lb.t_in else (lb, la)
                    names = (a.unit, b.unit) if lead is la else (b.unit, a.unit)
                    gap_in = foll.t_in - (lead.t_in + lead.pass_min)
                    gap_out = foll.t_out - (lead.t_out + lead.pass_min)
                    if min(gap_in, gap_out) < h - TOL_MIN:
                        rep.findings.append(Finding("SCH", rid, "T1", "violation", f"{names[0]}-{names[1]} on {la.arc[0]}",
                                                    f"time gap {min(gap_in, gap_out):.1f} min < {h:g} min"))
                elif la.arc == (lb.arc[0], -lb.arc[1]) and not can_meet(H, la.arc[0], ga.profile.width_m, gb.profile.width_m):
                    n += 1
                    if la.t_in < lb.tail_out - TOL_MIN and lb.t_in < la.tail_out - TOL_MIN:
                        rep.findings.append(Finding("SCH", rid, "T1", "violation", f"{a.unit}-{b.unit} on {la.arc[0]}",
                                                    "opposite columns meet on a single-lane road"))
        pa = {l.start: (i, l) for i, l in enumerate(a.legs)}
        for i, lb in enumerate(b.legs):  # junctions
            if lb.start not in pa:
                continue
            j, la = pa[lb.start]
            ain = a.legs[j - 1].arc if j else None
            bin_ = b.legs[i - 1].arc if i else None
            if (ain is not None and ain == bin_) or la.arc == lb.arc:
                continue
            if ain is not None and bin_ is not None and bin_ == (la.arc[0], -la.arc[1]) and lb.arc == (ain[0], -ain[1]):
                continue
            n += 1
            c = JUNCTION_CLEARANCE_MIN
            if la.t_in < lb.t_in + lb.pass_min + c - TOL_MIN and lb.t_in < la.t_in + la.pass_min + c - TOL_MIN:
                rep.findings.append(Finding("SCH", rid, "T1", "violation", f"{a.unit}-{b.unit} at {H.label(lb.start)}",
                                            "crossing columns pass the junction at the same time"))
    occupancy: dict[Any, list[tuple[float, float, int, str]]] = {}
    for c in credits:
        g = groups[c.group]
        drive = 0.0
        max_drive = g.active.param("RTE.GAP_MAX", "max_drive_min", None)
        gap_id = next((r.id for r in g.active.of("RTE.GAP_MAX")), None)
        for i, l in enumerate(c.legs):
            if l.halt_before > 0:
                drive = 0.0
            drive += l.t_out - l.t_in - l.stop_min  # a breakdown stop inside the leg is not driving
            n += 1
            if max_drive is not None and drive > max_drive + TOL_MIN:
                rep.findings.append(Finding("RTE", gap_id, "T1", "violation", c.unit, f"{drive:.0f} min of driving without a halt (max {max_drive:g})"))
                drive = 0.0
            stop = l.halt_before + (l.wait_before if i else 0.0)
            if stop > 1e-6:
                cap = G.nodes[l.start].get("halt_capacity")
                for r in g.active.of("SCH.HALT"):
                    if r.p("halt_areas_only") and not cap and l.halt_before > 0:
                        rep.findings.append(Finding("SCH", r.id, r.tier, "violation", c.unit, f"halt at {H.label(l.start)}, not a halt area"))
                if cap:
                    occupancy.setdefault(l.start, []).append((l.t_in - stop, l.t_in, c.n, c.unit))
                for r in g.active.of("PRO.CRITPOINT"):
                    kinds = set(r.p("element_kinds"))
                    for e in H.elements.values():
                        if e.kind in kinds and point_distance(G, node_xy(G, l.start), e.xy) <= r.p("no_stop_within_m"):
                            rep.findings.append(Finding("PRO", r.id, r.tier, "violation" if r.tier == "T1" else "shortfall", c.unit,
                                                        f"stops at {H.label(l.start)}, within {r.p('no_stop_within_m')} m of {e.name}"))
            ae = g.coster.arcs.get(l.arc)
            cap_kmh = g.active.param("SCH.MARCH", "max_speed_kmh", None)
            if ae is not None:
                rid = next((r.id for r in g.active.of("SCH.MARCH") if r.params.get("max_speed_kmh") == cap_kmh), "SCH.MARCH")
                if l.t_out - l.t_in < ae.time_min - TICK - 1e-6:
                    rep.findings.append(Finding("SCH", rid, "T1", "violation", c.unit, f"{l.arc[0]} driven faster than the march speed allows"))
                speed = ae.length_m / 1000 / max(ae.time_min / 60, 1e-9)
                if cap_kmh and speed > cap_kmh + 0.5:
                    rep.findings.append(Finding("SCH", rid, "T1", "violation", c.unit, f"{speed:.0f} km/h on {l.arc[0]} > {cap_kmh} km/h"))
            for r in g.active.of("SCH.WINDOW"):
                if r.p("area") != "built_up" or not any(H.G[u][v].get("built_up") for u, v in H.arc_edges(l.arc)):
                    continue
                s, e = r.p("start_min"), r.p("end_min")
                if l.t_in < e and s < l.tail_out:
                    rep.findings.append(Finding("SCH", r.id, r.tier, "violation" if r.tier == "T1" else "shortfall", c.unit,
                                                f"in a built-up area on {l.arc[0]} inside the window"))
        lim = g.active.param("SCH.MARCH", "max_unit_vehicles", None)
        if lim and c.n > lim:
            rep.findings.append(Finding("SCH", "SCH.MARCH", "T1", "violation", c.unit, f"{c.n} vehicles > {lim} per march unit"))
        if c.deadline is not None:
            n += 1
            if c.arrive > c.deadline + TOL_MIN:
                rep.findings.append(Finding("SCH", "ORDER", "T1", "violation", c.unit, f"arrives {c.arrive - c.deadline:.0f} min after the deadline"))
    for node, stays in occupancy.items():
        cap = G.nodes[node]["halt_capacity"]
        events = sorted([(s, nveh) for s, _, nveh, _ in stays] + [(e, -nveh) for _, e, nveh, _ in stays])
        present = 0
        for _, dv in events:
            present += dv
            if present > cap:
                rep.findings.append(Finding("SCH", "SCH.HALT", "T1", "violation", H.label(node), f"{present} vehicles in a halt area of {cap}"))
                break
    rep.checked["SCH"] = rep.checked.get("SCH", 0) + n


def spof_pairs(H: CorridorGraph, design: Design) -> dict[str, list[str]]:
    """(group -> elements) whose loss leaves the group without a designated route."""
    out: dict[str, list[str]] = {}
    for gid, msr in design.msr.items():
        routes = [msr] + design.asrs.get(gid, [])
        out[gid] = [c for c in msr.ordered_elements(H) if all(c in r.elements for r in routes)]
    return out


__all__ = ["Finding", "VerificationReport", "spof_pairs", "verify"]

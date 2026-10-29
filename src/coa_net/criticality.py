"""S4: criticality of critical elements and loss scenarios (methodology 5, S4).

Structural impact (exact, no model). For element c and group g:

    Delta_g(c) = T*(g; H minus c) - T*(g; H)        (+inf if no route within the guard rho)
    I(c) = sum_g w_g n_g min(Delta_g(c), Delta_max) + F |{g : Delta_g(c) = +inf}|

An element with Delta_g(c) = +inf is an *irreducible* single point of
failure for g: no routing removes it, only engineering, reconnaissance or
accepting the risk.

Likelihood. The red team (role L4, a language model) sorts element cards into
high / medium / low likelihood classes; two samples are merged by Borda
count; a calibration table turns classes into probabilities. Without a model,
a table by element type is used. Reported threats are fused:
pi_c <- 1 - (1 - pi_c)(1 - P_c).

Scenarios. omega_0 (no loss) plus the single loss of every element with
pi_c >= pi_min or an impact in the top decile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .candidates import CandidateSet
from .corridors import CorridorGraph
from .llm import call_validated
from .mission import Group

TYPE_LIKELIHOOD = {"bridge": 0.10, "tunnel": 0.10, "defile": 0.05}
OTHER_LIKELIHOOD = 0.02
CLASS_PROBABILITY = {"high": 0.30, "medium": 0.10, "low": 0.02}
DELTA_MAX_MIN = 120.0
STRANDED_PENALTY = 1000.0  # F in I(c), per stranded group
UNPREPARED_DELAY_MIN = 30.0  # extra time when a group falls back on a route that was not designated
FAILURE_TIME_MIN = 600.0  # F_g: no route at all


@dataclass
class Scenario:
    id: str
    lost: frozenset
    p: float
    label: str


@dataclass
class Criticality:
    delta: dict[tuple[str, str], float]
    irreducible: dict[str, set[str]]
    impact: dict[str, float]
    likelihood: dict[str, float]
    likelihood_source: dict[str, str]
    reasons: dict[str, str]
    scenarios: list[Scenario]
    fallback: dict[tuple[str, str], tuple[float, float, Any]]  # (group, scenario) -> (time, exposure, route or None)
    rho: dict[str, float]
    best_time: dict[tuple[str, str], float] = field(default_factory=dict)  # T*_g(omega): best possible time in the scenario
    notes: list[str] = field(default_factory=list)
    base_likelihood: dict[str, float] = field(default_factory=dict)  # before fusion with reported threats

    def scenario(self, sid: str) -> Scenario:
        return next(s for s in self.scenarios if s.id == sid)


def structural_impact(H: CorridorGraph, groups: dict[str, Group], cands: dict[str, CandidateSet]) -> tuple[dict, dict, dict, dict]:
    delta: dict[tuple[str, str], float] = {}
    irreducible: dict[str, set[str]] = {g: set() for g in groups}
    rho: dict[str, float] = {}
    for gid, g in groups.items():
        cs = cands[gid]
        rho[gid] = g.active.param("RTE.DETOUR", "rho", float("inf"))
        if cs.fastest is None:
            continue
        t0 = cs.fastest.time_min
        for e in cs.elements:
            if e in cs.articulation:
                delta[(gid, e)] = float("inf")
                irreducible[gid].add(e)
                continue
            rep = cs.replacement.get(e)
            if rep is None or rep.time_min > rho[gid] * t0 + 1e-6:
                delta[(gid, e)] = float("inf")
                irreducible[gid].add(e)
            else:
                delta[(gid, e)] = rep.time_min - t0
    impact: dict[str, float] = {}
    for (gid, e), d in delta.items():
        g = groups[gid]
        add = STRANDED_PENALTY if d == float("inf") else g.weight * g.n * min(d, DELTA_MAX_MIN)
        impact[e] = impact.get(e, 0.0) + add
    return delta, irreducible, impact, rho


def element_threat(H: CorridorGraph, eid: str) -> float:
    el = H.elements[eid]
    G = H.G
    if el.kind == "articulation":
        n = next(iter(el.nodes))
        vals = [G[n][m]["threat"] for m in G[n]]
        return max(vals, default=0.0)
    vals = []
    for u, v in el.edges:
        d = G[u][v] if G.has_edge(u, v) else G[v][u]
        vals.append(max(d.get("threat", 0.0), d.get("closed_p", 0.0)))
    return max(vals, default=0.0)


def table_likelihood(H: CorridorGraph, eids: list[str]) -> dict[str, float]:
    return {e: TYPE_LIKELIHOOD.get(H.elements[e].kind, OTHER_LIKELIHOOD) for e in eids}


RED_TEAM_SYSTEM = (
    "You are an intelligence analyst assessing likely enemy action against friendly logistic routes during the "
    "mission window. Rank only the listed element identifiers into likelihood classes high, medium or low, with at "
    "most five 'high'. Give each reason by citing fields of its card. Never output probabilities, places or coordinates."
)


def red_team(H: CorridorGraph, cards: dict[str, str], threat_summary: str, llm, samples: int = 2) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Role L4: likelihood classes from element cards; Borda merge of ``samples`` answers."""
    schema = {
        "type": "object",
        "properties": {
            "ranking": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}, "class": {"type": "string", "enum": ["high", "medium", "low"]}, "reason": {"type": "string"}},
                    "required": ["id", "class", "reason"],
                },
            }
        },
        "required": ["ranking"],
    }
    fields = ("surroundings", "nearest", "threat", "carries", "impact", "mlc", "clr", "bridge", "tunnel", "open", "forest", "built-up")

    def validate(out):
        problems = []
        rows = out.get("ranking", []) if isinstance(out, dict) else []
        seen = {}
        for r in rows:
            if r.get("id") not in cards:
                problems.append(f"unknown id {r.get('id')!r}")
                continue
            if r.get("class") not in CLASS_PROBABILITY:
                problems.append(f"bad class for {r['id']}")
                continue
            if not any(f in str(r.get("reason", "")).lower() for f in fields):
                problems.append(f"reason for {r['id']} cites no card field")
                continue
            seen[r["id"]] = (r["class"], r["reason"])
        if sum(1 for c, _ in seen.values() if c == "high") > 5:
            problems.append("more than five 'high'")
        if not seen:
            problems.append("empty ranking")
        return seen, problems

    user = "Threat summary: " + threat_summary + "\n\nElement cards:\n" + "\n\n".join(cards.values())
    answers = []
    log = []
    for i in range(samples):
        val, problems = call_validated(llm, RED_TEAM_SYSTEM, user + ("" if i == 0 else "\n\n(Independent second assessment.)"), schema, "red_team", validate)
        if val is None:
            log.append(f"red team sample {i + 1} rejected: {'; '.join(problems[:3])}")
        else:
            answers.append(val)
    if not answers:
        return {}, {}, log
    score = {e: 0.0 for e in cards}
    rank_value = {"high": 2, "medium": 1, "low": 0}
    for ans in answers:  # Borda count over classes
        for e in cards:
            score[e] += rank_value.get(ans.get(e, ("low", ""))[0], 0)
    classes, reasons = {}, {}
    for e, s in score.items():
        avg = s / len(answers)
        classes[e] = "high" if avg >= 1.5 else ("medium" if avg >= 0.5 else "low")
        reasons[e] = next((a[e][1] for a in answers if e in a), "")
    return classes, reasons, log


def assess(
    H: CorridorGraph,
    groups: dict[str, Group],
    cands: dict[str, CandidateSet],
    llm=None,
    pi_min: float = 0.05,
    max_scenarios: int = 40,
    threat_summary: str = "no enemy activity reported",
    base_likelihood: dict[str, float] | None = None,
) -> Criticality:
    """``base_likelihood`` reuses an earlier assessment (before threat fusion), e.g. when re-planning a what-if."""
    delta, irreducible, impact, rho = structural_impact(H, groups, cands)
    eids = sorted(impact, key=lambda e: (-impact[e], e))
    notes: list[str] = []
    likelihood = table_likelihood(H, eids)
    source = {e: "type table" for e in eids}
    reasons = {e: f"{H.elements[e].kind.replace('_', ' ')}: table value" for e in eids}
    if base_likelihood:
        for e in eids:
            if e in base_likelihood:
                likelihood[e] = base_likelihood[e]
                source[e] = "earlier assessment"
    elif llm is not None and eids:
        top = eids[:40]
        cards = {e: H.element_card(e, carries=_carries(e, delta), impact=_impact_text(e, delta), threats=_threat_text(H, e)) for e in top}
        classes, why, log = red_team(H, cards, threat_summary, llm)
        notes.extend(log)
        for e, cl in classes.items():
            likelihood[e] = CLASS_PROBABILITY[cl]
            source[e] = f"red team: {cl}"
            reasons[e] = why.get(e, "")
    base = dict(likelihood)
    for e in eids:
        pe = element_threat(H, e)
        if pe > 0:
            likelihood[e] = 1 - (1 - likelihood[e]) * (1 - pe)
            source[e] += f" + reported threat {pe:.2f}"
    top_decile = set(eids[: max(1, len(eids) // 10)]) if eids else set()
    chosen = [e for e in eids if likelihood[e] >= pi_min or e in top_decile][:max_scenarios]
    total = sum(likelihood[e] for e in chosen)
    scale = 1.0 / total if total > 1.0 else 1.0
    scenarios = [Scenario("S0", frozenset(), max(0.0, 1.0 - total * scale), "no loss")]
    for e in chosen:
        scenarios.append(Scenario(f"L-{e}", frozenset([e]), likelihood[e] * scale, f"loss of {H.elements[e].name} ({e})"))
    fallback: dict[tuple[str, str], tuple[float, float, Any]] = {}
    best_time: dict[tuple[str, str], float] = {}
    for gid in groups:
        cs = cands[gid]
        if cs.fastest is None:
            continue
        best_time[(gid, "S0")] = cs.fastest.time_min
        for s in scenarios[1:]:
            (e,) = tuple(s.lost)
            if e not in cs.fastest.elements:
                best_time[(gid, s.id)] = cs.fastest.time_min
            else:
                rep = cs.replacement.get(e)
                best_time[(gid, s.id)] = rep.time_min if rep is not None and e not in cs.articulation else FAILURE_TIME_MIN
        for s in scenarios[1:]:
            (e,) = tuple(s.lost)
            rep = cs.replacement.get(e)
            if e in cs.articulation or (rep is None and e in cs.elements):
                fallback[(gid, s.id)] = (FAILURE_TIME_MIN, 0.0, None)
            elif rep is not None:
                fallback[(gid, s.id)] = (rep.time_min + UNPREPARED_DELAY_MIN, rep.exposure, rep)
            elif cs.fastest is not None:
                fallback[(gid, s.id)] = (cs.fastest.time_min + UNPREPARED_DELAY_MIN, cs.fastest.exposure, cs.fastest)
    return Criticality(delta, irreducible, impact, likelihood, source, reasons, scenarios, fallback, rho, best_time=best_time, notes=notes,
                       base_likelihood=base)


def _carries(e: str, delta: dict) -> str:
    return ", ".join(sorted(g for (g, x) in delta if x == e)) or "none"


def _impact_text(e: str, delta: dict) -> str:
    parts = []
    for (g, x), d in sorted(delta.items()):
        if x == e:
            parts.append(f"{g} no route within the guard" if d == float("inf") else f"{g} +{d:.1f} min")
    return "; ".join(parts) or "none"


def _threat_text(H: CorridorGraph, e: str) -> str:
    t = element_threat(H, e)
    return f"threat level {t:.2f} on the element" if t > 0 else "none reported"


def impact_table(H: CorridorGraph, crit: Criticality) -> list[dict]:
    rows = []
    for e, imp in sorted(crit.impact.items(), key=lambda kv: -kv[1]):
        rows.append({
            "element": e,
            "name": H.elements[e].name,
            "kind": H.elements[e].kind,
            "impact": round(imp, 1),
            "likelihood": round(crit.likelihood.get(e, 0.0), 3),
            "source": crit.likelihood_source.get(e, ""),
            "groups": {g: ("no route within the guard" if d == float("inf") else round(d, 1)) for (g, x), d in crit.delta.items() if x == e},
        })
    return rows


__all__ = ["Criticality", "Scenario", "assess", "impact_table", "red_team", "structural_impact"]

"""S9: COA facts table and briefing (role L5; methodology S9, Appendix C.3).

Code computes one column of facts per COA: priority order, main and alternate
routes per group, designated length, control posts, single points of failure
(irreducible and accepted), the objective values, simulated on-time
probabilities, reconnaissance tasks, unknowns, and what each COA gains and
costs against the default COA. A language model may write the comparison (at
most 250 words) and a list of concerns; every number and identifier it uses
must appear in the facts, otherwise it is regenerated once, then replaced by
the templated comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .corridors import CorridorGraph
from .criticality import Criticality
from .design import ETA, OBJECTIVE_INFO, Design, coa_label
from .llm import call_validated, unsupported
from .mission import MissionContext
from .refine import Refinement
from .verify import VerificationReport

SHOWN = ["lateness", "threat_exposure", "resilience", "doctrine", "hazmat_builtup", "builtup", "travel_time", "network_size", "cover", "preferences"]


@dataclass
class COAFacts:
    name: str
    label: str
    description: str
    order: list[str]
    routes: dict[str, dict[str, Any]]  # group -> {"MSR": "LION 16.1 km 20.4 min", "ASR": [...]}
    designated_km: float
    posts: int
    irreducible: list[str]
    accepted: list[str]
    objectives: dict[str, float]
    on_time: dict[str, float] = field(default_factory=dict)
    recce: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)
    verification: str = ""

    def text(self) -> str:
        lines = [f"COA {self.label}: {self.description}", "order: " + " > ".join(self.order)]
        for g, r in self.routes.items():
            lines.append(f"{g}: MSR {r['MSR']}; ASR {', '.join(r['ASR']) or 'none'}")
        lines.append(f"designated {self.designated_km:.1f} km; control posts {self.posts}")
        lines.append("irreducible single points of failure: " + (", ".join(self.irreducible) or "none"))
        lines.append("accepted single points of failure: " + (", ".join(self.accepted) or "none"))
        lines.append("; ".join(f"{k} {self.objectives.get(k, 0.0):.1f} {OBJECTIVE_INFO[k][0]}" for k in SHOWN))
        if self.on_time:
            lines.append("simulated on-time probability: " + ", ".join(f"{u} {p:.2f}" for u, p in self.on_time.items()))
        if self.unknowns:
            lines.append("unknowns: " + "; ".join(self.unknowns))
        if self.recce:
            lines.append("reconnaissance: " + "; ".join(self.recce))
        if self.verification:
            lines.append("verification: " + self.verification)
        return "\n".join(lines)


def _route_text(H: CorridorGraph, ref: Refinement | None, r) -> str:
    nm = ref.name_of(r) if ref is not None else None
    els = [H.elements[e].name for e in r.ordered_elements(H) if H.elements[e].kind != "articulation"]
    via = f" via {', '.join(els)}" if els else ""
    return f"{nm + ' ' if nm else ''}{r.length_m / 1000:.1f} km {r.time_min:.1f} min{via}"


def _unknowns(ctx: MissionContext, d: Design) -> list[str]:
    """Unknown attributes on the designated routes, priced as risk: "detail (requirement) for groups"."""
    seen: dict[str, set[str]] = {}
    for g in d.msr:
        for r in d.routes(g):
            for u, v in zip(r.nodes[:-1], r.nodes[1:]):
                for rid, detail in ctx.groups[g].coster.evaluate(u, v).unknown:
                    seen.setdefault(f"{detail} ({rid})", set()).add(g)
    return [f"{k} for {', '.join(sorted(v))}" for k, v in sorted(seen.items())]


def facts_table(H: CorridorGraph, ctx: MissionContext, designs: list[Design], refs: dict[str, Refinement], crit: Criticality,
                sims: dict[str, Any] | None = None, recce: dict[str, list] | None = None,
                reports: dict[str, VerificationReport] | None = None) -> list[COAFacts]:
    out = []
    for d in designs:
        ref = refs.get(d.name)
        routes = {g: {"MSR": _route_text(H, ref, r), "ASR": [_route_text(H, ref, a) for a in d.asrs.get(g, [])]} for g, r in d.msr.items()}
        irr = [f"{g}: {H.elements[e].name}" for g, r in d.msr.items() for e in r.ordered_elements(H)
               if e in d.irreducible.get(g, set()) and H.elements[e].kind != "articulation"]
        acc = [f"{g}: {H.elements[e].name}" for g, s in d.accepted.items() for e in sorted(s) if H.elements[e].kind != "articulation"]
        unknowns = _unknowns(ctx, d)
        sim = (sims or {}).get(d.name)
        on_time = {u: round(sim.on_time(u), 2) for u in sorted({k for r in sim.reps for k in r.units})} if sim is not None else {}
        rec = [f"{t.id} {t.name} ({'in time' if t.feasible else 'not in time'})" for t in (recce or {}).get(d.name, [])]
        rep = (reports or {}).get(d.name)
        out.append(COAFacts(d.name, coa_label(d), d.description, list(d.order), routes, d.designated_length_km(H),
                            len(ref.posts) if ref else 0, irr, acc, dict(d.objectives), on_time, rec, unknowns,
                            rep.summary() if rep is not None else ""))
    return out


def facts_markdown(facts: list[COAFacts]) -> str:
    head = "| | " + " | ".join(f"COA {f.label}" for f in facts) + " |"
    rows = [head, "| --- |" + " --- |" * len(facts)]
    rows.append("| Priority order | " + " | ".join(" > ".join(f.order[:4]) + " ..." for f in facts) + " |")
    groups = sorted({g for f in facts for g in f.routes})
    for g in groups:
        rows.append(f"| {g} MSR | " + " | ".join(f.routes.get(g, {}).get("MSR", "-") for f in facts) + " |")
        rows.append(f"| {g} ASR | " + " | ".join("; ".join(f.routes.get(g, {}).get("ASR", [])) or "none" for f in facts) + " |")
    rows.append("| Designated length | " + " | ".join(f"{f.designated_km:.1f} km" for f in facts) + " |")
    rows.append("| Control posts | " + " | ".join(str(f.posts) for f in facts) + " |")
    rows.append("| Irreducible SPOFs | " + " | ".join(", ".join(f.irreducible) or "none" for f in facts) + " |")
    rows.append("| Accepted SPOFs | " + " | ".join(", ".join(f.accepted) or "none" for f in facts) + " |")
    for k in SHOWN:
        rows.append(f"| {k} ({OBJECTIVE_INFO[k][0]}) | " + " | ".join(f"{f.objectives.get(k, 0.0):.1f}" for f in facts) + " |")
    if any(f.on_time for f in facts):
        rows.append("| On time (simulated) | " + " | ".join(", ".join(f"{u} {p:.2f}" for u, p in f.on_time.items()) or "-" for f in facts) + " |")
    if any(f.unknowns for f in facts):
        rows.append("| Unknowns (priced as risk) | " + " | ".join("; ".join(f.unknowns) or "none" for f in facts) + " |")
    if any(f.recce for f in facts):
        rows.append("| Reconnaissance | " + " | ".join("; ".join(f.recce) or "none" for f in facts) + " |")
    if any(f.verification for f in facts):
        rows.append("| Verification | " + " | ".join(f.verification or "-" for f in facts) + " |")
    return "\n".join(rows)


def _diffs(a: COAFacts, b: COAFacts) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Objectives on which b is better (gains) and worse (costs) than a, in a's priority order."""
    order = ["lateness"] + [k for k in a.order if k != "lateness"]
    gains, costs = [], []
    for k in order:
        d = b.objectives.get(k, 0.0) - a.objectives.get(k, 0.0)
        if d < -ETA.get(k, 0.1):
            gains.append((k, -d))
        elif d > ETA.get(k, 0.1):
            costs.append((k, d))
    return gains, costs


def template_comparison(facts: list[COAFacts], default: str) -> tuple[str, list[str]]:
    base = next(f for f in facts if f.name == default)
    lines = [f"COA {base.label} follows the confirmed order ({', '.join(base.order[:3])}, ...)."]
    for f in facts:
        if f.name == base.name:
            continue
        gains, costs = _diffs(base, f)
        if not gains and not costs:
            lines.append(f"COA {f.label} gives the same values.")
            continue
        g = ", ".join(f"{k} by {v:.1f} {OBJECTIVE_INFO[k][0]}" for k, v in gains[:2]) or "nothing"
        c = ", ".join(f"{k} by {v:.1f} {OBJECTIVE_INFO[k][0]}" for k, v in costs[:2]) or "nothing"
        lines.append(f"COA {f.label} ({f.description.split(':')[0]}) gains {g} and costs {c} against COA {base.label}.")
        diff_routes = [g_ for g_ in f.routes if f.routes[g_]["MSR"] != base.routes.get(g_, {}).get("MSR")]
        for g_ in diff_routes[:2]:
            lines.append(f"  {g_}: MSR {f.routes[g_]['MSR']} instead of {base.routes[g_]['MSR']}.")
    concerns = []
    for f in facts:
        for s in f.irreducible:
            concerns.append(f"COA {f.label}: {s} is an irreducible single point of failure")
        for s in f.unknowns[:3]:
            concerns.append(f"COA {f.label}: {s}, priced as risk")
        for r in f.recce:
            if "not in time" in r:
                concerns.append(f"COA {f.label}: reconnaissance {r}")
    return "\n".join(lines), sorted(set(concerns))


L5_SYSTEM = (
    "You compare courses of action (COAs) for a military logistic movement for the commander. Compare in the "
    "confirmed priority order; use only numbers from the facts; state each trade-off as 'COA R gains X on resilience "
    "for Y minutes of travel'; every concern cites an element, route or group from the facts. Do not recommend beyond "
    "saying which COA follows the confirmed order. At most 250 words."
)


def briefing(facts: list[COAFacts], default: str, llm=None) -> tuple[str, list[str], str]:
    """Comparison, concerns and source ("template" or "model")."""
    text, concerns = template_comparison(facts, default)
    if llm is None:
        return text, concerns, "template"
    table = "\n\n".join(f.text() for f in facts)
    schema = {"type": "object", "properties": {"comparison": {"type": "string"}, "concerns": {"type": "array", "items": {"type": "string"}}},
              "required": ["comparison", "concerns"]}

    def validate(out):
        if not isinstance(out, dict):
            return None, ["not an object"]
        comp = str(out.get("comparison", ""))
        cons = [str(c) for c in out.get("concerns", [])]
        problems = []
        if len(comp.split()) > 250:
            problems.append("more than 250 words")
        bad = unsupported(comp + "\n" + "\n".join(cons), table)
        if bad:
            problems.append("not in the facts: " + ", ".join(bad[:8]))
        return (comp, cons), problems

    got, _ = call_validated(llm, L5_SYSTEM, "Facts:\n" + table, schema, "coa", validate)
    if got is None:
        return text, concerns, "template"
    return got[0], got[1], "model"


__all__ = ["COAFacts", "briefing", "facts_markdown", "facts_table", "template_comparison"]

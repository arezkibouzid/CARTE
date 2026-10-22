"""C5: symbolic verifier and the verify-relax-replan loop.

The verifier re-evaluates every active rule on the produced plan, from the
graph and the rule base, independently of the optimiser's cached costs. It
reports hard violations, unknown map data, network and temporal violations,
and time-space conflicts between convoys, plus the metrics of section 7 of
the proposal.

When a convoy is infeasible, :func:`blocking_rules` names the hard rules that
disconnect it. Only rules marked ``relaxable`` may be turned into heavily
weighted soft terms, either chosen by the LLM or by default; anything else is
escalated to the operator. The loop is capped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import networkx as nx

from .expr import eval_feature, eval_predicate
from .graph import resolve, ukey
from .optimizer import EdgeCoster, overlap, plan_network
from .schemas import ConstraintProgram, MissionSpec, Plan, Rule, SoftTerm, Violation
from .vehicles import ConvoyProfile

if TYPE_CHECKING:
    from .llm import LLMClient

RELAXED_WEIGHT = 5.0


@dataclass
class Report:
    violations: list[Violation] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)

    @property
    def hard(self) -> list[Violation]:
        return [v for v in self.violations if v.kind in ("hard", "closed")]

    @property
    def ok(self) -> bool:
        return not any(v.kind in ("hard", "closed", "conflict") for v in self.violations)

    def summary(self) -> str:
        kinds: dict[str, int] = {}
        for v in self.violations:
            kinds[v.kind] = kinds.get(v.kind, 0) + 1
        return f"{'VALID' if self.ok else 'INVALID'} | " + ", ".join(f"{k}={n}" for k, n in sorted(kinds.items()))


def verify(
    G: nx.DiGraph,
    plan: Plan,
    program: ConstraintProgram,
    profiles: dict[str, ConvoyProfile],
    mission: MissionSpec,
    rules: list[Rule] | None = None,
) -> Report:
    rep = Report()
    by_id = {r.id: r for r in rules or []}
    convoys = {c.id: c for c in mission.convoys}
    soft_sat, delivered, total_time, total_len, n_alt = [], 0, 0.0, 0.0, []
    unknown_edges = 0
    for cid, cp in plan.convoys.items():
        cc = program.convoys[cid]
        veh = profiles[cid].as_vehicle()
        if cp.msr is None:
            rep.violations.append(Violation(convoy_id=cid, rule_id="INFEASIBLE", kind="hard", detail="; ".join(cp.messages)))
            continue
        hard_rules = list(cc.hard)
        for rid in cc.relaxed:
            if rid in by_id:
                hard_rules.append(by_id[rid])
        for label, route in [("MSR", cp.msr)] + [(f"ASR{i + 1}", r) for i, r in enumerate(cp.asrs)]:
            for u, v in route.edges():
                if not G.has_edge(u, v):
                    rep.violations.append(Violation(convoy_id=cid, rule_id="GRAPH", kind="hard", detail=f"{label}: edge missing", edge=(u, v)))
                    continue
                d = G[u][v]
                for r in hard_rules:
                    ok = eval_predicate(r.predicate, d, veh)
                    if ok is None and r.on_unknown == "forbid":
                        ok = False
                    if ok is False:
                        relaxed = r.id in cc.relaxed
                        rep.violations.append(
                            Violation(
                                convoy_id=cid,
                                rule_id=r.id,
                                kind="relaxed" if relaxed else ("closed" if r.id == "H-CLOSED" else "hard"),
                                detail=f"{label}: {'relaxed rule' if relaxed else 'violated'} on {d.get('name') or (u, v)}",
                                edge=(u, v),
                            )
                        )
                    elif ok is None and label == "MSR":
                        unknown_edges += 1
                        rep.violations.append(
                            Violation(convoy_id=cid, rule_id=r.id, kind="unknown", detail=f"{label}: map data missing", edge=(u, v))
                        )
        # soft satisfaction on the MSR: length-weighted share with zero feature, averaged over rules
        if cc.soft and cp.msr.length_m > 0:
            sats = []
            for t in cc.soft:
                exposure = sum(eval_feature(t.feature, G[u][v], veh) * G[u][v]["length_m"] for u, v in cp.msr.edges())
                sats.append(1.0 - exposure / cp.msr.length_m)
            soft_sat.append(sum(sats) / len(sats))
        need = int(cc.network.get("min_alternates", 0))
        n_alt.append(len(cp.asrs))
        if len(cp.asrs) < need:
            rep.violations.append(
                Violation(convoy_id=cid, rule_id="N-ALTERNATE", kind="network", detail=f"{len(cp.asrs)}/{need} alternates")
            )
        max_ov = cc.network.get("max_overlap")
        if max_ov is not None:
            for i, a in enumerate(cp.asrs):
                ov = overlap(G, a, cp.msr)
                if ov > max_ov + 1e-9:
                    rep.violations.append(
                        Violation(convoy_id=cid, rule_id="N-ALTERNATE", kind="network", detail=f"ASR{i + 1} overlaps MSR {ov:.0%}")
                    )
        late = convoys[cid].latest_arrival_min
        if late is not None and cp.arrival_min is not None and cp.arrival_min > late + 1e-6:
            rep.violations.append(
                Violation(convoy_id=cid, rule_id="TIME-WINDOW", kind="temporal", detail=f"arrives {cp.arrival_min:.0f} > {late:.0f} min")
            )
        else:
            delivered += 1
        total_time += cp.msr.travel_time_min
        total_len += cp.msr.length_m

    rep.violations.extend(_conflicts(plan, program))
    n = max(1, len(plan.convoys))
    rep.metrics = {
        "convoys": len(plan.convoys),
        "hard_violations": len(rep.hard),
        "unknown_msr_checks": unknown_edges,
        "conflicts": sum(v.kind == "conflict" for v in rep.violations),
        "soft_satisfaction": round(sum(soft_sat) / len(soft_sat), 4) if soft_sat else 1.0,
        "delivery_rate": delivered / n,
        "total_travel_time_min": round(total_time, 2),
        "total_length_km": round(total_len / 1000, 3),
        "mean_alternates": sum(n_alt) / len(n_alt) if n_alt else 0.0,
    }
    return rep


def _conflicts(plan: Plan, program: ConstraintProgram) -> list[Violation]:
    out = []
    occ: dict[tuple, list[tuple[float, float, str]]] = {}
    for cid, cp in plan.convoys.items():
        for s in cp.schedule:
            occ.setdefault(ukey(s.u, s.v), []).append((s.t_in, s.t_out, cid))
    seen = set()
    for key, iv in occ.items():
        iv.sort()
        for i, (a_in, a_out, a) in enumerate(iv):
            for b_in, b_out, b in iv[i + 1 :]:
                if a == b:
                    continue
                h = max(program.convoys[a].network.get("min_headway_min", 0), program.convoys[b].network.get("min_headway_min", 0))
                if b_in < a_out + h - 1e-6 and a_in < b_out + h - 1e-6 and (a, b, key) not in seen:
                    seen.add((a, b, key))
                    out.append(
                        Violation(convoy_id=b, rule_id="N-HEADWAY", kind="conflict", detail=f"{a} and {b} closer than {h:.0f} min", edge=key)
                    )
    return out


# ---------------------------------------------------------------------------
# Repair loop
# ---------------------------------------------------------------------------


def blocking_rules(G: nx.DiGraph, coster: EdgeCoster, s: Any, t: Any) -> list[str]:
    """Hard rules whose removal alone reconnects ``s`` to ``t``."""
    out = []
    for r in coster.cc.hard:
        ok_edges = [(u, v) for (u, v), e in coster.evals.items() if set(e.violated) <= {r.id}]
        H = nx.DiGraph(ok_edges)
        if s in H and t in H and nx.has_path(H, s, t):
            out.append(r.id)
    return out


def diagnose(G: nx.DiGraph, coster: EdgeCoster, s: Any, t: Any, by_id: dict[str, Rule]) -> tuple[list[str], list[Rule]]:
    """(blocking rule ids, the relaxable ones among them)."""
    blocking = blocking_rules(G, coster, s, t)
    return blocking, [by_id[b] for b in blocking if by_id.get(b) and by_id[b].relaxable]


def operator_message(blocking: list[str]) -> str:
    return (
        "operator decision needed: blocked by "
        + (", ".join(blocking) if blocking else "a combination of hard rules")
        + " (not auto-relaxable)"
    )


def relax(program: ConstraintProgram, convoy_id: str, rule: Rule, reason: str) -> None:
    cc = program.convoys[convoy_id]
    cc.hard = [r for r in cc.hard if r.id != rule.id]
    cc.relaxed.append(rule.id)
    cc.soft.append(SoftTerm(rule_id=rule.id, feature=f"not ({rule.predicate})", weight=RELAXED_WEIGHT))
    program.notes.append(f"{convoy_id}: relaxed {rule.id} -> soft (w={RELAXED_WEIGHT}): {reason}")


def plan_verify_repair(
    G: nx.DiGraph,
    mission: MissionSpec,
    program: ConstraintProgram,
    profiles: dict[str, ConvoyProfile],
    rules: list[Rule],
    llm: "LLMClient | None" = None,
    max_iter: int = 3,
    deconflict: bool = True,
    costers: dict[str, EdgeCoster] | None = None,
) -> tuple[Plan, Report]:
    by_id = {r.id: r for r in rules}
    convoys = {c.id: c for c in mission.convoys}
    plan = plan_network(G, mission, program, profiles, deconflict=deconflict, costers=costers)
    report = verify(G, plan, program, profiles, mission, rules)
    for it in range(max_iter):
        infeasible = [cid for cid, cp in plan.convoys.items() if cp.msr is None]
        if not infeasible:
            break
        changed = False
        for cid in infeasible:
            c = convoys[cid]
            coster = EdgeCoster(G, program.convoys[cid], profiles[cid], program.unknown_penalty)
            s, t = resolve(G, c.origin), resolve(G, c.destination)
            blocking, cands = diagnose(G, coster, s, t, by_id)
            if not cands:
                plan.convoys[cid].messages.append(operator_message(blocking))
                continue
            choice, why = _choose_relaxation(cands, c, llm)
            relax(program, cid, choice, why)
            changed = True
        if not changed:
            break
        if costers is not None:
            costers.clear()
        plan = plan_network(G, mission, program, profiles, deconflict=deconflict, costers=costers)
        plan.log.append(f"repair iteration {it + 1}: {program.notes[-1] if program.notes else ''}")
        report = verify(G, plan, program, profiles, mission, rules)
    return plan, report


def _choose_relaxation(cands: list[Rule], convoy, llm: "LLMClient | None") -> tuple[Rule, str]:
    default = (cands[0], "default: first relaxable blocking rule")
    if llm is None or len(cands) == 1:
        return cands[0], "only relaxable rule blocking the mission" if len(cands) == 1 else default[1]
    schema = {
        "type": "object",
        "properties": {"rule_id": {"type": "string", "enum": [r.id for r in cands]}, "justification": {"type": "string"}},
        "required": ["rule_id", "justification"],
    }
    try:
        out = llm.complete_json(
            "You advise a military logistics planner. Pick the doctrine rule whose relaxation is least harmful.",
            json.dumps({"convoy": convoy.model_dump(mode="json"), "blocking_rules": [r.model_dump(mode="json") for r in cands]}),
            schema,
            stage="relaxation",
        )
        chosen = next(r for r in cands if r.id == out["rule_id"])
        return chosen, out.get("justification", "")
    except Exception:  # noqa: BLE001
        return default

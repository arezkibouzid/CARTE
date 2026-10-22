"""C7: doctrine-grounded explanations.

For each convoy: binding vehicle characteristics, the chosen MSR with the
rules and map attributes that shaped it, the doctrine-blind shortest path and
the exact rules it would break, alternates, and every relaxation or delay.
"""

from __future__ import annotations

from typing import Any

import networkx as nx

from .expr import eval_predicate
from .graph import resolve
from .optimizer import overlap, shortest
from .schemas import ConstraintProgram, MissionSpec, Plan, Route
from .vehicles import ConvoyProfile


def hhmm(t: float | None) -> str:
    if t is None:
        return "--:--"
    t = int(round(t))
    return f"{(t // 60) % 24:02d}:{t % 60:02d}" + (f" (D+{t // 1440})" if t >= 1440 else "")


def roads_used(G: nx.DiGraph, route: Route) -> str:
    names: list[str] = []
    for u, v in route.edges():
        d = G[u][v]
        n = d.get("name") or d.get("highway", "road")
        if isinstance(n, list):
            n = n[0]
        if not names or names[-1] != n:
            names.append(str(n))
    return " > ".join(names)


def _hard_breaks(G, route_nodes, hard, vehicle) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for u, v in zip(route_nodes[:-1], route_nodes[1:]):
        d = G[u][v]
        for r in hard:
            if eval_predicate(r.predicate, d, vehicle) is False:
                out.setdefault(r.id, []).append(d.get("name") or f"{u}-{v}")
    return out


def explain_plan(
    G: nx.DiGraph,
    plan: Plan,
    mission: MissionSpec,
    program: ConstraintProgram,
    profiles: dict[str, ConvoyProfile],
    rules_by_id: dict[str, Any] | None = None,
) -> str:
    rules_by_id = rules_by_id or {}
    lines = [f"# Plan {plan.mission_id} v{plan.version} ({mission.phase})", ""]
    for c in sorted(mission.convoys, key=lambda c: (c.priority, c.id)):
        cp = plan.convoys[c.id]
        p = profiles[c.id]
        cc = program.convoys[c.id]
        veh = ", ".join(f"{g.count} x {g.type}" for g in c.vehicles)
        lines += [
            f"## Convoy {c.id} (priority {c.priority}): {veh}",
            f"{c.origin} -> {c.destination}. Binding profile: MLC {p.mlc}, {p.weight_t:g} t, "
            f"{p.width_m:g} m wide, {p.height_m:g} m high, max slope {p.max_slope_pct:g} %, {p.n_vehicles} vehicles"
            + (f", cargo {sorted(p.cargo)}" if p.cargo else "")
            + ".",
            "",
        ]
        if cp.msr is None:
            lines += [f"**No feasible route.** {' '.join(cp.messages)}", ""]
            continue
        r = cp.msr
        lines += [
            f"**Status {cp.status}.** Depart {hhmm(cp.departure_min)}, arrive {hhmm(cp.arrival_min)}"
            + (f" (deadline {hhmm(c.latest_arrival_min)})" if c.latest_arrival_min is not None else "")
            + f". MSR {r.length_m / 1000:.1f} km, {r.travel_time_min:.0f} min driving"
            + (f", {len(cp.halts)} halt(s)" if cp.halts else "")
            + ".",
            f"- Roads: {roads_used(G, r)}",
        ]
        drivers = sorted(r.features.items(), key=lambda kv: -kv[1])
        if drivers:
            lines.append("- Residual exposure on the MSR (length-weighted):")
            for rid, exp in drivers:
                if rid.startswith("unknown:"):
                    lines.append(f"  - {rid[8:]}: map data missing on {exp / 1000:.1f} km (priced as risk, check on the ground)")
                else:
                    rat = getattr(rules_by_id.get(rid), "rationale", "")
                    lines.append(f"  - {rid}: {exp / 1000:.2f} km-equivalent. {rat}")
        # why not the doctrine-blind shortest path
        s, t = resolve(G, c.origin), resolve(G, c.destination)
        sp = shortest(G, s, t, "length_m")
        if sp and sp != r.nodes:
            sp_len = sum(G[u][v]["length_m"] for u, v in zip(sp[:-1], sp[1:]))
            breaks = _hard_breaks(G, sp, cc.hard, p.as_vehicle())
            why = "; ".join(f"{rid} on {', '.join(sorted(set(where)))[:120]}" for rid, where in breaks.items())
            lines.append(
                f"- Rejected: the shortest path ({sp_len / 1000:.1f} km, {(r.length_m - sp_len) / 1000:+.1f} km vs MSR) "
                + (f"breaks {why}." if why else "is legal but scores worse on the soft doctrine terms above.")
            )
        for i, a in enumerate(cp.asrs):
            lines.append(
                f"- ASR{i + 1}: {a.length_m / 1000:.1f} km, {a.travel_time_min:.0f} min, overlap with MSR {overlap(G, a, r):.0%}, "
                f"via {roads_used(G, a)[:160]}"
            )
        for m in cp.messages:
            lines.append(f"- Note: {m}")
        if cc.relaxed:
            lines.append(f"- Relaxed rules (operator to confirm): {', '.join(cc.relaxed)}")
        lines.append(f"- Active rules: {', '.join(sorted([x.id for x in cc.hard] + [x.rule_id for x in cc.soft]))}")
        lines.append("")
    if program.notes:
        lines += ["## Planner notes", *[f"- {n}" for n in program.notes], ""]
    if plan.log:
        lines += ["## Log", *[f"- {n}" for n in plan.log], ""]
    return "\n".join(lines)

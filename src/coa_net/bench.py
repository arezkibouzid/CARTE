"""Benchmark on the synthetic family F1 (methodology 10.2-10.6): baselines, COA-Net, and the incident loop.

Planning. For every seed: the synthetic theatre generator and its random mission, then B0-B1, COA-Net
with the greedy design (ablation A5-like, no CP-SAT) and COA-Net with CP-SAT, all under the default
priority order. Every plan gets the same movement-credit scheduler and the same Monte Carlo script
(one loss, uniform over the critical elements designated by any pipeline of that seed, common random
numbers). COA-Net plans carry their playbooks; the baselines react only.

Metrics, per pipeline (means over seeds, with 95 % bootstrap intervals for the main ones):

* road-valid: share of seeds with no T0/T1 violation of the road and route levels (SEG, RTE) on the
  designated routes; valid: no T0/T1 violation at any level (network survivability, schedule, protection);
* SPOF groups: share of groups whose main route crosses a bridge or tunnel that every designated
  alternate also crosses, and the same for reducible elements only;
* ASR share and ASR/MSR time ratio;
* objectives of the default order (resilience, threat exposure, travel time, network size);
* simulated on-time probability, weighted delay and share of replications where a column reached a
  lost element before knowing of the loss;
* design time.

Incident loop. For every seed, COA-Net's plan and one random incident 10 minutes after the first
departure (bridge destroyed, road blocked or threat zone), handled with and without playbooks (A7):
mode, computation time, validity after the repair.
"""

from __future__ import annotations

import random
import statistics
import time
from typing import Any

from .anticipation import build_playbooks
from .baselines import BASELINES
from .candidates import generate_candidates
from .corridors import CorridorGraph
from .criticality import assess
from .design import DEFAULT_EPS, DEFAULT_ORDER, Design, DesignProblem, solve
from .incidents import PlanState, handle_incident
from .mission import build_context
from .refine import refine
from .requirements import load_requirements
from .scenarios import random_incident, random_mission, synthetic_theatre
from .schedule import schedule
from .simulate import monte_carlo, plan_elements, plan_from, script_uniform
from .verify import verify

PIPELINES = ["B0", "B1", "COA-Net (greedy)", "COA-Net"]


def _crossings(H: CorridorGraph, r) -> set[str]:
    return {e for e in r.elements if H.elements[e].kind in ("bridge", "tunnel")}


def network_metrics(H: CorridorGraph, d: Design, crit) -> dict[str, float]:
    n = len(d.msr)
    spof = red = with_asr = 0
    ratios = []
    for g, m in d.msr.items():
        asrs = d.asrs.get(g, [])
        shared = {e for e in _crossings(H, m) if all(e in a.elements for a in asrs)}
        spof += bool(shared)
        red += bool({e for e in shared if e not in crit.irreducible.get(g, set())})
        if asrs:
            with_asr += 1
            ratios.append(min(a.time_min for a in asrs) / max(m.time_min, 1e-9))
    return {
        "groups": n,
        "spof_groups": spof / n if n else 0.0,
        "reducible_spof_groups": red / n if n else 0.0,
        "asr_share": with_asr / n if n else 0.0,
        "asr_ratio": statistics.fmean(ratios) if ratios else float("nan"),
    }


def run_planning_bench(seeds, n_convoys: int | None = None, reps: int = 50, use_cpsat: bool = True,
                       time_limit_s: float = 10.0) -> dict[str, list[dict]]:
    reqs = load_requirements()
    results: dict[str, list[dict]] = {p: [] for p in PIPELINES}
    for seed in seeds:
        th = synthetic_theatre(seed)
        H = CorridorGraph(th.G)
        m = random_mission(th, seed, n_convoys)
        ctx = build_context(H, m, reqs)
        cands = {gid: generate_candidates(H, g) for gid, g in ctx.groups.items()}
        crit = assess(H, ctx.groups, cands)
        prob = DesignProblem(H, ctx.groups, cands, crit, phase=m.phase)
        designs: dict[str, tuple[Design, float]] = {}
        for name, fn in BASELINES.items():
            t = time.perf_counter()
            designs[name] = (fn(prob), time.perf_counter() - t)
        for name, cp in (("COA-Net (greedy)", False), ("COA-Net", True)):
            if cp and not use_cpsat:
                continue
            t = time.perf_counter()
            d = solve(prob, list(DEFAULT_ORDER), dict(DEFAULT_EPS), "P", time_limit_s=time_limit_s, use_cpsat=cp)
            designs[name] = (d, time.perf_counter() - t)
        plans = {}
        rows = {}
        for name, (d, secs) in designs.items():
            if not d.msr:
                continue
            ref = refine(H, ctx.groups, d, crit)
            sc = schedule(H, ctx, d.msr, use_cpsat=use_cpsat)
            rep = verify(H, ctx, d, ref, sc, crit)
            books = build_playbooks(H, ctx, d, crit, ref, use_cpsat=use_cpsat) if name.startswith("COA-Net") else None
            plans[name] = plan_from(H, ctx, d, sc, books, name=name)
            road_ok = not any(f.level in ("SEG", "RTE") for f in rep.violations)
            row: dict[str, Any] = {"seed": seed, "convoys": len(m.convoys), "seconds": secs, "valid": float(rep.ok), "valid_roads": float(road_ok),
                                   "violations": len(rep.violations), "unplanned": len(d.unplanned)}
            row.update(network_metrics(H, d, crit))
            for k in ("resilience", "threat_exposure", "travel_time", "network_size", "hazmat_builtup"):
                row[k] = d.objectives.get(k, 0.0)
            rows[name] = row
        if reps > 0 and plans:
            start = min(g.earliest for g in ctx.groups.values())
            script = script_uniform(plan_elements(H, list(plans.values())), (start, start + 30.0), seed=seed)
            for name, p in plans.items():
                res = monte_carlo(H, ctx, p, script, reps=reps, seed=seed)
                rows[name].update({"on_time": res.on_time(), "weighted_delay": statistics.fmean(res.weighted_delay(r) for r in res.reps),
                                   "reached_closed": res.reached_closed(), "completion": res.completion()})
        for name, row in rows.items():
            results[name].append(row)
    return results


def run_incident_bench(seeds, n_convoys: int = 3, use_cpsat: bool = True) -> dict[str, list[dict]]:
    reqs = load_requirements()
    out: dict[str, list[dict]] = {"with playbooks": [], "without playbooks (A7)": []}
    for seed in seeds:
        for label in out:
            th = synthetic_theatre(seed)
            H = CorridorGraph(th.G)
            m = random_mission(th, seed, n_convoys)
            ctx = build_context(H, m, reqs)
            cands = {gid: generate_candidates(H, g) for gid, g in ctx.groups.items()}
            crit = assess(H, ctx.groups, cands)
            prob = DesignProblem(H, ctx.groups, cands, crit, phase=m.phase)
            d = solve(prob, list(DEFAULT_ORDER), dict(DEFAULT_EPS), "P", use_cpsat=use_cpsat)
            if not d.msr:
                continue
            ref = refine(H, ctx.groups, d, crit)
            sc = schedule(H, ctx, d.msr, use_cpsat=use_cpsat)
            books = build_playbooks(H, ctx, d, crit, ref, use_cpsat=use_cpsat) if label == "with playbooks" else []
            st = PlanState(H, ctx, reqs, d.order, d.eps, d, crit, ref, sc, books, use_cpsat=use_cpsat)
            t0 = min(g.earliest for g in ctx.groups.values()) + 10.0
            inc = random_incident(th, seed, t0)
            o = handle_incident(st, inc)
            out[label].append({"seed": seed, "incident": inc.type.value, "mode": o.mode, "seconds": o.seconds,
                               "valid": float(o.verification.ok) if o.verification is not None else 1.0,
                               "affected": len(o.affected)})
    return out


def _mean(rows: list[dict], k: str) -> float:
    xs = [r[k] for r in rows if k in r and r[k] == r[k]]
    return statistics.fmean(xs) if xs else float("nan")


def bootstrap_ci(xs: list[float], n: int = 1000, seed: int = 0) -> tuple[float, float]:
    xs = [x for x in xs if x == x]
    if len(xs) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choice(xs) for _ in xs) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def to_markdown(results: dict[str, list[dict]], title: str) -> str:
    cols = [("valid_roads", "road-valid", "{:.0%}"), ("valid", "valid (all levels)", "{:.0%}"), ("spof_groups", "SPOF groups", "{:.0%}"), ("reducible_spof_groups", "reducible SPOF groups", "{:.0%}"),
            ("asr_share", "groups with ASR", "{:.0%}"), ("asr_ratio", "ASR/MSR time", "{:.2f}"), ("resilience", "resilience (veh.min)", "{:.1f}"),
            ("threat_exposure", "exposure", "{:.2f}"), ("travel_time", "travel time (veh.min)", "{:.0f}"), ("network_size", "network km", "{:.1f}"),
            ("on_time", "on time (sim)", "{:.1%}"), ("weighted_delay", "weighted delay (sim)", "{:.1f}"),
            ("reached_closed", "reached lost element (sim)", "{:.1%}"), ("seconds", "design s", "{:.2f}")]
    n = max((len(v) for v in results.values()), default=0)
    lines = [f"### {title}", "", f"{n} seeds; means over seeds.", "",
             "| pipeline | " + " | ".join(c[1] for c in cols) + " |", "| --- |" + " --- |" * len(cols)]
    for name, rows in results.items():
        if not rows:
            continue
        cells = []
        for k, _, fmt in cols:
            v = _mean(rows, k)
            cells.append("-" if v != v else fmt.format(v))
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines += ["", "95 % bootstrap intervals (over seeds):", ""]
    for k in ("reducible_spof_groups", "resilience", "weighted_delay"):
        parts = []
        for name, rows in results.items():
            lo, hi = bootstrap_ci([r[k] for r in rows if k in r])
            if lo == lo:
                parts.append(f"{name} [{lo:.2f}, {hi:.2f}]")
        if parts:
            lines.append(f"- {k}: " + "; ".join(parts))
    return "\n".join(lines)


def incidents_markdown(results: dict[str, list[dict]], title: str) -> str:
    lines = [f"### {title}", "", "| variant | incidents | modes | mean s | max s | valid after |", "| --- | --- | --- | --- | --- | --- |"]
    for name, rows in results.items():
        if not rows:
            continue
        modes: dict[str, int] = {}
        for r in rows:
            modes[r["mode"]] = modes.get(r["mode"], 0) + 1
        ms = ", ".join(f"{k} {v}" for k, v in sorted(modes.items()))
        lines.append(f"| {name} | {len(rows)} | {ms} | {_mean(rows, 'seconds'):.2f} | {max(r['seconds'] for r in rows):.2f} | {_mean(rows, 'valid'):.0%} |")
    return "\n".join(lines)


__all__ = ["PIPELINES", "bootstrap_ci", "incidents_markdown", "network_metrics", "run_incident_bench", "run_planning_bench", "to_markdown"]

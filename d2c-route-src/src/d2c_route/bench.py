"""Experiment harness (proposal section 8): baselines, ablations, repair study.

Runs on synthetic theatres with planted constraints, so violations are
measurable without expert labels. Every method is verified against the *full*
doctrine program, whatever it optimised.
"""

from __future__ import annotations

import statistics
import time
from typing import Callable

from .constraints import activate, load_rules
from .incidents import repair
from .optimizer import EdgeCoster, plan_network
from .scenarios import random_incident, random_mission, synthetic_theatre
from .schemas import ConstraintProgram
from .vehicles import convoy_profile
from .verifier import Report, plan_verify_repair, verify


def _strip(program: ConstraintProgram, hard: bool, soft: bool, network: bool) -> ConstraintProgram:
    p = program.model_copy(deep=True)
    for cc in p.convoys.values():
        if not hard:
            cc.hard = []
        if not soft:
            cc.soft = []
        if not network:
            cc.network = {}
    return p


def _row(rep: Report, seconds: float) -> dict[str, float]:
    bad = {v.convoy_id for v in rep.violations if v.kind in ("hard", "closed")}
    m = rep.metrics
    return {
        "invalid_convoy_share": len(bad) / max(1, m["convoys"]),
        "hard_violations": m["hard_violations"],
        "conflicts": m["conflicts"],
        "network_violations": sum(v.kind == "network" for v in rep.violations),
        "soft_satisfaction": m["soft_satisfaction"],
        "mean_alternates": m["mean_alternates"],
        "delivery_rate": m["delivery_rate"],
        "travel_time_min": m["total_travel_time_min"],
        "runtime_s": seconds,
    }


def run_planning_bench(seeds: range | list[int], n_convoys: int | None = None) -> dict[str, list[dict]]:
    rules = load_rules()
    results: dict[str, list[dict]] = {}
    for seed in seeds:
        th = synthetic_theatre(seed)
        mission = random_mission(th, seed, n_convoys)
        profiles = {c.id: convoy_profile(c) for c in mission.convoys}
        full = activate(rules, mission, profiles)

        methods: dict[str, Callable] = {
            "B1 fastest path (no doctrine)": lambda: (plan_network(th.G, mission, _strip(full, False, False, False), profiles, deconflict=False), full),
            "B2 vehicle-aware (hard rules only)": lambda: (plan_network(th.G, mission, _strip(full, True, False, False), profiles, deconflict=False), full),
            "D2C w/o deconfliction": lambda: _d2c(th.G, mission, full, profiles, rules, deconflict=False),
            "D2C-Route (full)": lambda: _d2c(th.G, mission, full, profiles, rules, deconflict=True),
        }
        for name, fn in methods.items():
            t0 = time.perf_counter()
            plan, prog = fn()
            dt = time.perf_counter() - t0
            rep = verify(th.G, plan, prog, profiles, mission, rules)
            results.setdefault(name, []).append(_row(rep, dt))
    return results


def _d2c(G, mission, full, profiles, rules, deconflict):
    prog = full.model_copy(deep=True)
    plan, _ = plan_verify_repair(G, mission, prog, profiles, rules, deconflict=deconflict)
    return plan, prog


def run_repair_bench(seeds: range | list[int], n_convoys: int = 4) -> dict[str, list[dict]]:
    """Local, stability-aware repair vs full re-plan after a random incident."""
    rules = load_rules()
    results: dict[str, list[dict]] = {}
    for seed in seeds:
        th = synthetic_theatre(seed)
        mission = random_mission(th, seed, n_convoys)
        profiles = {c.id: convoy_profile(c) for c in mission.convoys}
        for mode in ("local", "full"):
            G = th.G.copy()
            prog = activate(rules, mission, profiles)
            costers: dict[str, EdgeCoster] = {}
            plan, _ = plan_verify_repair(G, mission, prog, profiles, rules, costers=costers)
            t_inc = min(c.earliest_departure_min for c in mission.convoys) + 8
            inc = random_incident(th, seed, t_inc)
            new, rr = repair(G, plan, mission, prog, profiles, inc, mode=mode, costers=costers)
            rep = verify(G, new, prog, profiles, mission, rules)
            results.setdefault(f"{mode} repair", []).append(
                {
                    "latency_s": rr.latency_s,
                    "replanned_convoys": len(rr.affected),
                    "unchanged_share": rr.unchanged_share,
                    "hard_violations": rep.metrics["hard_violations"],
                    "conflicts": rep.metrics["conflicts"],
                    "delivery_rate": rep.metrics["delivery_rate"],
                }
            )
    return results


def to_markdown(results: dict[str, list[dict]], title: str) -> str:
    cols = list(next(iter(results.values()))[0])
    out = [f"### {title}", "", "| method | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for name, rows in results.items():
        cells = []
        for c in cols:
            vals = [r[c] for r in rows]
            mu = statistics.fmean(vals)
            sd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
            cells.append(f"{mu:.3g} ± {sd:.2g}" if sd else f"{mu:.3g}")
        out.append(f"| {name} | " + " | ".join(cells) + " |")
    n = len(next(iter(results.values())))
    out += ["", f"Mean ± std over {n} synthetic scenarios."]
    return "\n".join(out)

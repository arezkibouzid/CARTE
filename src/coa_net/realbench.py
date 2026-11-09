"""The 100-seed benchmark of :mod:`coa_net.bench`, on a real road network instead of the synthetic theatre.

For every seed, a random mission between the towns of a gazetteer (1 to 4 convoys, the synthetic benchmark's
vehicle mixes, departures 06:00-07:00, three hours to arrive), planned by the baselines B0 and B1 and by COA-Net
(CP-SAT), each scheduled and verified the same way and simulated with the same Monte Carlo loss script. The
metrics are those of the synthetic benchmark (validity, single points of failure, ASR, objectives, simulated
on-time probability and delay, design time).

Run it on several networks of the same area (BD TOPO, the image network, the image network with the learned
corrector) with the same seeds: the missions are the same, so the plans can be compared. For a network read
from the image, every designated route is also scored against BD TOPO (share of its length within 25 m and
50 m of a BD TOPO road): the shapefile checks the plan, it never feeds it.
"""

from __future__ import annotations

import math
import random
import statistics
import time
from typing import Any

from .anticipation import build_playbooks
from .baselines import BASELINES
from .bench import network_metrics
from .candidates import generate_candidates
from .corridors import CorridorGraph
from .criticality import assess
from .design import DEFAULT_EPS, DEFAULT_ORDER, DesignProblem, solve
from .mission import build_context
from .schemas import Convoy, LocationRef, MissionSpec, VehicleGroup
from .refine import refine
from .scenarios import VEHICLE_MIXES
from .schedule import schedule
from .simulate import monte_carlo, plan_elements, plan_from, script_uniform
from .verify import verify

PIPELINES = ["B0", "B1", "COA-Net"]


def random_real_mission(places: dict[str, tuple[float, float]], seed: int, n_convoys: int | None = None,
                        min_km: float = 25.0) -> MissionSpec:
    """Seeded random mission between named towns at least ``min_km`` apart (same seed, same mission)."""
    rng = random.Random(seed)
    names = sorted(places)
    n = n_convoys or rng.randint(1, 4)

    def km(a, b):
        (x1, y1), (x2, y2) = places[a], places[b]
        return math.hypot((x2 - x1) * 111.32 * math.cos(math.radians((y1 + y2) / 2)), (y2 - y1) * 110.57)

    convoys = []
    for i in range(n):
        for _ in range(100):
            a, b = rng.sample(names, 2)
            if km(a, b) >= min_km:
                break
        mix = rng.choice(VEHICLE_MIXES)
        dep = 360 + 10 * rng.randint(0, 6)
        convoys.append(Convoy(id=f"C{i + 1}", vehicles=[VehicleGroup(type=t, count=k) for t, k in mix],
                              origin=LocationRef(name=a), destination=LocationRef(name=b), earliest_departure_min=dep,
                              latest_arrival_min=dep + 180, priority=rng.randint(1, 3),
                              cargo=["fuel"] if any(t == "fuel_tanker" for t, _ in mix) else []))
    return MissionSpec(id=f"real-{seed}", phase=rng.choice(["day", "night"]), convoys=convoys)


def route_lines(H: CorridorGraph, route) -> list:
    """lon/lat polylines of a route (one per road section)."""
    G = H.G
    out = []
    for u, v in zip(route.nodes[:-1], route.nodes[1:]):
        if G.has_edge(u, v):
            c = G.edges[u, v].get("coords") or ((G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"]))
            out.append(c)
    return out


class BDScorer:
    """Share of a set of lon/lat lines within 25 m and 50 m of a BD TOPO road (check only)."""

    def __init__(self, bdtopo: str):
        import pyproj
        from scipy.spatial import cKDTree

        from .scancheck import _points, reference_lines

        bd, _ = reference_lines(bdtopo)
        xy, _ = _points(bd.geometry.values, 10.0)
        self.tree = cKDTree(xy)
        self.tr = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)

    def score(self, lines: list) -> dict:
        import numpy as np
        import shapely

        if not lines:
            return {}
        g = []
        for c in lines:
            a = np.asarray(c, float)
            if len(a) >= 2:
                xs, ys = self.tr.transform(a[:, 0], a[:, 1])
                g.append(shapely.LineString(np.column_stack([xs, ys])))
        seg = shapely.segmentize(np.asarray(g), 10.0)
        xy, idx = shapely.get_coordinates(seg, return_index=True)
        d, _ = self.tree.query(xy, distance_upper_bound=60.0)
        return {"bd_25m": float((d <= 25).mean()), "bd_50m": float((d <= 50).mean())}


def run(G, places_nodes: dict[str, Any], seeds, max_rank: int | None, reqs, reps: int = 20, time_limit_s: float = 10.0,
        scorer: BDScorer | None = None, n_convoys: int | None = None, places_xy=None, progress=print) -> dict[str, list[dict]]:
    """Plan every seed's mission on ``G`` (towns already named on the graph) with B0, B1 and COA-Net."""
    import copy

    results: dict[str, list[dict]] = {p: [] for p in PIPELINES}
    for seed in seeds:
        t_seed = time.perf_counter()
        # a fresh network for every seed, as the synthetic benchmark does: planning a mission edits the graph
        # (terminal connectors, closures), and a later seed must not see what an earlier one left
        H = CorridorGraph(copy.deepcopy(G), max_rank=max_rank)
        m = random_real_mission(places_xy, seed, n_convoys)
        try:
            ctx = build_context(H, m, reqs)
        except Exception as e:  # a town not reachable on this network
            progress(f"  seed {seed}: mission not built ({e})")
            continue
        t = time.perf_counter()
        cands = {gid: generate_candidates(H, g) for gid, g in ctx.groups.items()}
        t_cand = time.perf_counter() - t
        crit = assess(H, ctx.groups, cands)
        prob = DesignProblem(H, ctx.groups, cands, crit, phase=m.phase)
        designs = {}
        for name, fn in BASELINES.items():
            if name not in PIPELINES:
                continue
            t = time.perf_counter()
            designs[name] = (fn(prob), time.perf_counter() - t)
        t = time.perf_counter()
        designs["COA-Net"] = (solve(prob, list(DEFAULT_ORDER), dict(DEFAULT_EPS), "P", time_limit_s=time_limit_s, use_cpsat=True),
                              time.perf_counter() - t)
        plans, rows = {}, {}
        for name, (d, secs) in designs.items():
            try:
                _plan_row(H, ctx, m, crit, name, d, secs, t_cand, seed, scorer, plans, rows)
            except Exception as e:  # recorded, not fatal: one failing plan must not stop the benchmark
                rows[name] = {"seed": seed, "convoys": len(m.convoys), "planned": 0.0, "valid": 0.0, "valid_roads": 0.0, "error": repr(e)}
                progress(f"  seed {seed} {name}: error {e!r}")
        if reps > 0 and plans:
            start = min(g.earliest for g in ctx.groups.values())
            script = script_uniform(plan_elements(H, list(plans.values())), (start, start + 30.0), seed=seed)
            for name, p in plans.items():
                res = monte_carlo(H, ctx, p, script, reps=reps, seed=seed)
                rows[name].update({"on_time": res.on_time(), "weighted_delay": statistics.fmean(res.weighted_delay(r) for r in res.reps),
                                   "reached_closed": res.reached_closed()})
        for name, row in rows.items():
            results[name].append(row)
        progress(f"  seed {seed}: {len(m.convoys)} convoys, {time.perf_counter() - t_seed:.0f} s ({len(H.corridors)} corridors); COA-Net valid "
                 f"{rows.get('COA-Net', {}).get('valid')}")
    return results


def _plan_row(H, ctx, m, crit, name, d, secs, t_cand, seed, scorer, plans, rows) -> None:
    """Refine, schedule, verify and (COA-Net) playbooks for one design; one row of metrics."""
    if not d.msr:
        rows[name] = {"seed": seed, "convoys": len(m.convoys), "planned": 0.0, "valid": 0.0, "valid_roads": 0.0}
        return
    ref = refine(H, ctx.groups, d, crit)
    sc = schedule(H, ctx, d.msr, use_cpsat=True)
    rep = verify(H, ctx, d, ref, sc, crit)
    books = build_playbooks(H, ctx, d, crit, ref, use_cpsat=True) if name == "COA-Net" else None
    plans[name] = plan_from(H, ctx, d, sc, books, name=name)
    road_ok = not any(f.level in ("SEG", "RTE") for f in rep.violations)
    row: dict[str, Any] = {"seed": seed, "convoys": len(m.convoys), "planned": 1.0 - len(d.unplanned) / max(len(ctx.groups), 1),
                           "seconds": secs, "candidates_s": t_cand, "valid": float(rep.ok), "valid_roads": float(road_ok),
                           "violations": len(rep.violations)}
    row.update(network_metrics(H, d, crit))
    for k in ("resilience", "threat_exposure", "travel_time", "network_size", "hazmat_builtup"):
        row[k] = d.objectives.get(k, 0.0)
    row["msr"] = {g: {"km": r.length_m / 1000, "min": r.time_min} for g, r in d.msr.items()}
    if scorer is not None:
        row.update(scorer.score([ln for r in d.msr.values() for ln in route_lines(H, r)]))
    rows[name] = row

"""Route-level scoring of the learned corrector: does a correction bring the image network's routes closer to
BD TOPO's?

The road-level gate of :mod:`coa_net.scanlearn` asks whether the changes a correction makes are right. This one
asks whether they matter the right way: on pairs of points drawn in the blocks of one fold, the fastest convoy
route (60 km/h cap, roads of rank 5 or better, doubtful links priced as the planner prices them) is computed on the
corrected image network and on BD TOPO, and the score is the mean absolute log ratio of their times plus that of
their lengths (0 when the image routes match BD TOPO's exactly; a route made too short counts as much as one too
long). A correction is kept when removing it makes the score worse on the whole and on at least ``min_folds``
folds. BD TOPO is the yardstick only: it never enters the image network.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import networkx as nx
import numpy as np

CAP_KMH = 60.0


def _csgraph(G: nx.DiGraph, priced: bool):
    """Sparse time graph of the roads of rank 5 or better, its largest strongly connected part, and the edge data."""
    import scipy.sparse as sp

    from .graph import speed_kmh
    from .scanlearn import uncertainty_penalty

    H = G.edge_subgraph([(u, v) for u, v, d in G.edges(data=True) if d.get("road_rank", 5) <= 5])
    big = max(nx.strongly_connected_components(H), key=len)
    nodes = list(big)
    ix = {n: i for i, n in enumerate(nodes)}
    r, c, w, t, L = [], [], [], [], []
    for u, v, d in H.edges(data=True):
        if u in ix and v in ix:
            tm = d["length_m"] / 1000.0 / min(speed_kmh(d), CAP_KMH) * 60.0
            pen = uncertainty_penalty(d) if priced and d.get("source_id") == "scan" else 1.0
            r.append(ix[u]); c.append(ix[v]); w.append(tm * pen); t.append(tm); L.append(d["length_m"])
    n = len(nodes)
    W = sp.csr_matrix((w, (r, c)), shape=(n, n))
    T = {(a, b): (tt, ll) for a, b, tt, ll in zip(r, c, t, L)}
    xy = np.array([[G.nodes[x]["x"], G.nodes[x]["y"]] for x in nodes])
    return W, T, xy, nodes


def route_table(G: nx.DiGraph, pairs: list[tuple], priced: bool = True) -> np.ndarray:
    """(time min, length km) of the fastest route of each pair of lon/lat points; NaN where no route."""
    from scipy.sparse.csgraph import dijkstra
    from scipy.spatial import cKDTree

    W, T, xy, nodes = _csgraph(G, priced)
    k = np.array([math.cos(math.radians(48.0)), 1.0])
    tree = cKDTree(xy * k)
    a = tree.query(np.array([p[0] for p in pairs]) * k)[1]
    b = tree.query(np.array([p[1] for p in pairs]) * k)[1]
    src, inv = np.unique(a, return_inverse=True)
    _, P = dijkstra(W, directed=True, indices=src, return_predecessors=True)
    out = np.full((len(pairs), 2), np.nan)
    for i in range(len(pairs)):
        pred = P[inv[i]]
        x, tt, ll, ok = b[i], 0.0, 0.0, True
        while x != a[i]:
            px = pred[x]
            if px < 0:
                ok = False
                break
            e = T[(px, x)]
            tt += e[0]
            ll += e[1]
            x = px
        if ok:
            out[i] = (tt, ll / 1000.0)
    return out


def fold_pairs(bounds: tuple, grid: int, folds_of_blocks, per_fold: int = 60, min_km: float = 10.0, max_km: float = 70.0,
               seed: int = 0) -> dict[int, list[tuple]]:
    """Random pairs of points, both in blocks of the same fold, ``min_km`` to ``max_km`` apart."""
    from .scanlearn import blocks_of

    rng = np.random.default_rng(seed)
    x0, y0, x1, y1 = bounds
    m = 0.02
    pts = np.column_stack([rng.uniform(x0 + m, x1 - m, 6000), rng.uniform(y0 + m, y1 - m, 6000)])
    fold = np.asarray(folds_of_blocks)[blocks_of(pts, bounds, grid)]
    out: dict[int, list[tuple]] = {}
    for f in np.unique(fold):
        p = pts[fold == f]
        got = []
        for _ in range(20000):
            i, j = rng.integers(0, len(p), 2)
            dk = math.hypot((p[i, 0] - p[j, 0]) * 111.32 * math.cos(math.radians(48.0)), (p[i, 1] - p[j, 1]) * 110.57)
            if min_km <= dk <= max_km:
                got.append((tuple(p[i]), tuple(p[j])))
            if len(got) >= per_fold:
                break
        out[int(f)] = got
    return out


def score(img: np.ndarray, bd: np.ndarray) -> float:
    ok = ~(np.isnan(img).any(1) | np.isnan(bd).any(1))
    if not ok.any():
        return float("inf")
    return float(np.mean(np.abs(np.log(img[ok, 0] / bd[ok, 0]))) + np.mean(np.abs(np.log(img[ok, 1] / bd[ok, 1]))))


def summary(img: np.ndarray, bd: np.ndarray) -> dict:
    ok = ~(np.isnan(img).any(1) | np.isnan(bd).any(1))
    rt, rl = img[ok, 0] / bd[ok, 0], img[ok, 1] / bd[ok, 1]
    return {"pairs": int(ok.sum()), "time_ratio_median": float(np.median(rt)), "length_ratio_median": float(np.median(rl)),
            "time_total_ratio": float(img[ok, 0].sum() / bd[ok, 0].sum()), "score": score(img, bd)}


def route_gate(G0: nx.DiGraph, P: dict, enabled: set, thresholds: dict, G_bd: nx.DiGraph, pairs: dict[int, list],
               gated: tuple = ("add_link", "white_to_rank4", "separate_crossing", "bridge_to_culvert"),
               tune: dict | None = None, min_folds: int = 3, precision_of=None, min_precision: float = 0.5,
               progress=print) -> dict:
    """Route gate of the corrections in ``gated`` that passed the road-level gate (``enabled``).

    ``tune``: decision -> (threshold key, candidate values); the value with the best route score is kept first.
    ``precision_of(decision, key, value)``: the share of right changes on unseen blocks at that threshold; values under
    ``min_precision`` are not tried. Returns the decisions kept, the thresholds chosen and the scores per fold."""
    from .scanlearn import apply_corrections

    tune = tune or {"add_link": ("link", (0.3, 0.4, 0.5, 0.6, 0.7)), "white_to_rank4": ("imp4", (0.4, 0.5, 0.6, 0.7))}
    flat = [p for f in sorted(pairs) for p in pairs[f]]
    fid = np.array([f for f in sorted(pairs) for _ in pairs[f]])
    bd = route_table(G_bd, flat, priced=False)
    cache: dict = {}

    def evaluate(on: frozenset, th: dict) -> dict:
        key = (on, tuple(sorted(th.items())))
        if key not in cache:
            G = copy.deepcopy(G0)
            apply_corrections(G, P, th, set(on))
            img = route_table(G, flat, priced=True)
            cache[key] = {"all": score(img, bd), "folds": {int(f): score(img[fid == f], bd[fid == f]) for f in np.unique(fid)},
                          "summary": summary(img, bd)}
        return cache[key]

    th = dict(thresholds)
    on = set(enabled)
    log = {"tuning": {}, "leave_one_out": {}}
    for dec, (k, values) in tune.items():
        if dec not in on:
            continue
        best = None
        for v in values:
            if precision_of is not None:
                pr = precision_of(dec, k, v)
                if pr < min_precision:  # a threshold where most changes are wrong is never kept, whatever the routes say
                    progress(f"  route gate: {dec} at {k} = {v}: precision {pr:.0%} on unseen blocks, below {min_precision:.0%}: skipped")
                    continue
            r = evaluate(frozenset(on), dict(th, **{k: v}))
            log["tuning"].setdefault(dec, {})[v] = r["all"]
            progress(f"  route gate: {dec} at {k} = {v}: score {r['all']:.4f}")
            if best is None or r["all"] < best[0]:
                best = (r["all"], v)
        th[k] = best[1]
    for dec in gated:
        if dec not in on:
            continue
        with_ = evaluate(frozenset(on), th)
        without = evaluate(frozenset(on - {dec}), th)
        better = sum(1 for f in with_["folds"] if with_["folds"][f] < without["folds"][f])
        keep = with_["all"] < without["all"] and better >= min_folds
        log["leave_one_out"][dec] = {"with": with_["all"], "without": without["all"], "folds_better": better,
                                     "folds": len(with_["folds"]), "kept": keep}
        progress(f"  route gate: {dec}: score {with_['all']:.4f} with, {without['all']:.4f} without, better on {better} of "
                 f"{len(with_['folds'])} folds -> {'kept' if keep else 'dropped'}")
        if not keep:
            on.discard(dec)
    none = evaluate(frozenset(), th)
    final = evaluate(frozenset(on), th)
    log.update({"enabled": sorted(on), "thresholds": th, "pairs": len(flat), "no_correction": none["summary"],
                "corrected": final["summary"]})
    return log


def markdown(gate: dict) -> str:
    """Report section of the route gate."""
    if not gate:
        return ""
    L = ["## Route gate", "",
         f"{gate['pairs']} pairs of points, both ends in blocks of the same fold; fastest convoy route (60 km/h cap, rank 5 or "
         "better, doubtful links priced) on the corrected image network against BD TOPO. Score: mean |log time ratio| + mean "
         "|log length ratio| (0 = the same routes as BD TOPO).", ""]
    for dec, vals in gate.get("tuning", {}).items():
        L.append(f"- threshold of {dec}: " + ", ".join(f"{v:g}: {s:.4f}" for v, s in vals.items()))
    L += ["", "| correction | score with | score without | folds where it helps | kept |", "| --- | --- | --- | --- | --- |"]
    for dec, r in gate.get("leave_one_out", {}).items():
        L.append(f"| {dec} | {r['with']:.4f} | {r['without']:.4f} | {r['folds_better']} of {r['folds']} | {'yes' if r['kept'] else 'no'} |")
    a, b = gate["no_correction"], gate["corrected"]
    L += ["", "| network | time / BD TOPO, median | length / BD TOPO, median | total time / BD TOPO | score |", "| --- | --- | --- | --- | --- |",
          f"| image, no correction (pricing only) | {a['time_ratio_median']:.3f} | {a['length_ratio_median']:.3f} | {a['time_total_ratio']:.3f} | {a['score']:.4f} |",
          f"| image, corrections kept | {b['time_ratio_median']:.3f} | {b['length_ratio_median']:.3f} | {b['time_total_ratio']:.3f} | {b['score']:.4f} |", ""]
    return "\n".join(L)

"""Learned displacement of the image network: move the lines back to where the roads are.

A 1:100 000 map draws its symbols wider than the roads and pushes them apart where they crowd (cartographic
generalisation), so the centre line read from the image sits a few tens of metres off the road. The displacement is
not random: it depends on what is drawn next to the road (another road, an expressway band, a river, lettering) and
on which side. This module learns it.

* **Samples**: points every ``STEP_M`` along each image road, at least ``END_M`` from its ends (junctions do not move,
  so the network stays connected).
* **Target** (BD TOPO, training and scoring only): the signed offset along the line's normal to the nearest BD TOPO
  road, when that road runs parallel (within 30 degrees) within 60 m; else no label.
* **Features** (image only): the symbol and its width, the class raster along the normal on each side (fills, casing,
  water, lettering, white, at 0-8, 8-16 and 16-32 pixels), the distance and symbol of the nearest other image line on
  each side, the curvature, the distance to the nearer end.
* **Model**: gradient-boosted regression, scored out of fold on the blocks of :mod:`coa_net.scanlearn`.
* **Moving**: the predicted offsets are smoothed along the line, tapered to zero at its ends, capped at ``MAX_M``,
  and every vertex is moved along the normal.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx
import numpy as np

STEP_M = 50.0
END_M = 40.0
MAX_M = 40.0
LABEL_M = 60.0
L93 = "EPSG:2154"


def _xy(tr, coords):
    c = np.asarray(coords, float)
    x, y = tr.transform(c[:, 0], c[:, 1])
    return np.column_stack([x, y])


def _resample(xy: np.ndarray, step: float):
    seg = np.hypot(*np.diff(xy, axis=0).T)
    ch = np.concatenate([[0.0], np.cumsum(seg)])
    L = ch[-1]
    if L < 2 * END_M + step:
        return None, None, L
    s = np.arange(END_M, L - END_M + 1e-6, step)
    x = np.interp(s, ch, xy[:, 0])
    y = np.interp(s, ch, xy[:, 1])
    return np.column_stack([x, y]), s, L


def _normals(xy_line: np.ndarray, s: np.ndarray, ch: np.ndarray, h: float = 20.0):
    """Unit tangent and left normal at chainages ``s`` (finite differences over +-h metres)."""
    a = np.column_stack([np.interp(np.clip(s - h, 0, ch[-1]), ch, xy_line[:, 0]), np.interp(np.clip(s - h, 0, ch[-1]), ch, xy_line[:, 1])])
    b = np.column_stack([np.interp(np.clip(s + h, 0, ch[-1]), ch, xy_line[:, 0]), np.interp(np.clip(s + h, 0, ch[-1]), ch, xy_line[:, 1])])
    t = b - a
    t /= np.maximum(np.hypot(t[:, 0], t[:, 1]), 1e-9)[:, None]
    return t, np.column_stack([-t[:, 1], t[:, 0]])


def samples(G: nx.DiGraph, roads, classes, transform):
    """Sample points of every road with their features (image only). Returns (table, per-road arrays)."""
    import pandas as pd
    import pyproj
    from scipy.spatial import cKDTree

    from .scanlearn import LINK_RANK, _sample, _to_px
    from .scanroads import C_BLACK, C_BLUE, C_CASING, C_CASING_LIGHT, C_ORANGE, C_RED, C_WATER, C_WHITE

    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    inv = pyproj.Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    lines = [_xy(tr, g.coords) for g in roads.geometry.values]
    # all image line points, with their road, for the nearest-other-line features
    allp, allr = [], []
    for i, xy in enumerate(lines):
        p, _, _ = _resample(xy, 10.0) if len(xy) >= 2 else (None, None, 0)
        if p is None:
            seg = np.hypot(*np.diff(xy, axis=0).T) if len(xy) > 1 else np.zeros(0)
            n = max(2, int(seg.sum() / 10.0) + 1)
            ch = np.concatenate([[0.0], np.cumsum(seg)])
            s = np.linspace(0, ch[-1], n) if ch[-1] > 0 else np.zeros(1)
            p = np.column_stack([np.interp(s, ch, xy[:, 0]), np.interp(s, ch, xy[:, 1])])
        allp.append(p)
        allr.append(np.full(len(p), i))
    allp = np.vstack(allp)
    allr = np.concatenate(allr)
    tree = cKDTree(allp)
    sym = roads["symbol"].values
    width = roads["width_px"].fillna(4.0).values
    rows, where = [], []
    bands = ((0, 8), (8, 16), (16, 32))
    cls_list = {"blue": (C_BLUE,), "fill": (C_RED, C_ORANGE), "casing": (C_CASING, C_CASING_LIGHT), "water": (C_WATER,),
                "black": (C_BLACK,), "white": (C_WHITE,)}
    for i, xy in enumerate(lines):
        if len(xy) < 2:
            continue
        p, s, L = _resample(xy, STEP_M)
        if p is None:
            continue
        seg = np.hypot(*np.diff(xy, axis=0).T)
        ch = np.concatenate([[0.0], np.cumsum(seg)])
        t, n = _normals(xy, s, ch)
        t2, _ = _normals(xy, s + 30.0, ch)
        t1, _ = _normals(xy, s - 30.0, ch)
        curv = np.arctan2(t1[:, 0] * t2[:, 1] - t1[:, 1] * t2[:, 0], (t1 * t2).sum(1))  # signed heading change, left > 0
        # image classes along the normal, both sides, in pixels
        lon, lat = inv.transform(p[:, 0], p[:, 1])
        px = _to_px(transform, np.column_stack([lon, lat]))
        # normal in pixel space (row, col): east -> col, north -> -row
        npx = np.column_stack([-n[:, 1], n[:, 0]])
        feats = {"symbol_rank": np.full(len(p), LINK_RANK.get(sym[i], 5)), "width_px": np.full(len(p), width[i]),
                 "curvature": curv, "to_end_m": np.minimum(s, L - s), "road_len_m": np.full(len(p), L)}
        for side, sg in (("left", 1.0), ("right", -1.0)):
            for a, b in bands:
                offs = np.arange(a, b, 1.0) + 1.0
                q = px[:, None, :] + sg * offs[None, :, None] * npx[:, None, :]
                cl = _sample(classes, q)
                for nm, cs in cls_list.items():
                    feats[f"{side}_{nm}_{a}_{b}"] = np.isin(cl, cs).mean(1)
        # nearest other image line on each side within 150 m (vectorised over the 24 nearest line points)
        dist, idx = tree.query(p, k=24, distance_upper_bound=150.0)
        valid = np.isfinite(dist)
        idx_c = np.where(valid, idx, 0)
        other = valid & (allr[idx_c] != i)
        v = allp[idx_c] - p[:, None, :]
        side_d = (v * n[:, None, :]).sum(-1)
        for side, sm in (("left", side_d > 0), ("right", side_d < 0)):
            m = other & sm
            dd = np.where(m, dist, np.inf)
            j = np.argmin(dd, axis=1)
            best = dd[np.arange(len(p)), j]
            has = np.isfinite(best)
            feats[f"{side}_line_m"] = np.where(has, best, 150.0)
            rk = np.array([LINK_RANK.get(x, 5) for x in sym[allr[idx_c[np.arange(len(p)), j]]]], float)
            feats[f"{side}_line_rank"] = np.where(has, rk, 9.0)
        rows.append(pd.DataFrame(feats))
        where.append((i, s, p, n))
    X = pd.concat(rows, ignore_index=True)
    return X, where


def labels(where, bdtopo: str):
    """Signed normal offset to the nearest parallel BD TOPO road within LABEL_M (NaN when none)."""
    from scipy.spatial import cKDTree

    from .scancheck import reference_lines
    from .scanlearn import _pts

    bd, _ = reference_lines(bdtopo, None)
    bxy, bidx = _pts(bd.geometry.values, 5.0)
    nxt = np.r_[bidx[1:] == bidx[:-1], False]
    prv = np.r_[False, bidx[1:] == bidx[:-1]]
    d = np.zeros_like(bxy)
    d[:-1][nxt[:-1]] = (bxy[1:] - bxy[:-1])[nxt[:-1]]
    fill = ~nxt & prv
    d[fill] = bxy[fill] - bxy[np.flatnonzero(fill) - 1]
    d /= np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)[:, None]
    T = cKDTree(bxy)
    P = np.vstack([w[2] for w in where])
    N = np.vstack([w[3] for w in where])
    tan = np.column_stack([N[:, 1], -N[:, 0]])
    dist, j = T.query(P, k=8, distance_upper_bound=LABEL_M)
    valid = np.isfinite(dist)
    jc = np.where(valid, j, 0)
    par = valid & (np.abs((d[jc] * tan[:, None, :]).sum(-1)) >= math.cos(math.radians(30)))
    first = np.argmax(par, axis=1)
    has = par[np.arange(len(P)), first]
    q = bxy[jc[np.arange(len(P)), first]]
    y = ((q - P) * N).sum(1)
    return np.where(has, y, np.nan)


def train_and_score(X, y, blocks, folds_of_blocks, n_folds: int = 4, seed: int = 0, progress=print):
    """Out-of-fold predictions and scores of the displacement model."""
    import lightgbm as lgb

    fold = np.asarray(folds_of_blocks)[blocks]
    m = ~np.isnan(y)
    oof = np.full(len(y), np.nan)
    params = dict(n_estimators=400, learning_rate=0.05, num_leaves=63, min_child_samples=40, subsample=0.8, subsample_freq=1,
                  colsample_bytree=0.8, verbose=-1, random_state=seed)
    for f in range(n_folds):
        tr, te = m & (fold != f), fold == f
        if tr.sum() < 100 or te.sum() == 0:
            continue
        mdl = lgb.LGBMRegressor(**params).fit(X[tr], y[tr])
        oof[te] = mdl.predict(X[te])
    final = lgb.LGBMRegressor(**params).fit(X[m], y[m])
    ok = m & ~np.isnan(oof)
    pred = np.clip(oof[ok], -MAX_M, MAX_M)
    before = np.abs(y[ok])
    after = np.abs(y[ok] - pred)
    sc = {"samples": int(m.sum()), "scored": int(ok.sum()), "mean_offset_m": float(before.mean()), "mean_after_m": float(after.mean()),
          "within_10m_before": float((before <= 10).mean()), "within_10m_after": float((after <= 10).mean()),
          "within_25m_before": float((before <= 25).mean()), "within_25m_after": float((after <= 25).mean()),
          "r2": float(1 - ((y[ok] - oof[ok]) ** 2).sum() / ((y[ok] - y[ok].mean()) ** 2).sum())}
    progress(f"  displacement: {sc['scored']} samples on unseen blocks, mean offset {sc['mean_offset_m']:.1f} m -> "
             f"{sc['mean_after_m']:.1f} m; within 10 m {sc['within_10m_before']:.0%} -> {sc['within_10m_after']:.0%}; R2 {sc['r2']:.2f}")
    return oof, final, sc


def offsets_by_road(roads, where, pred) -> dict:
    """{road key: [[chainage, offset], ...]} from per-sample predictions."""
    out, k = {}, 0
    for i, s, p, n in where:
        v = pred[k:k + len(s)]
        k += len(s)
        if np.isnan(v).all():
            continue
        g0 = roads.geometry.values[i].coords[0]
        out[roads["key"].values[i]] = {"start": [g0[0], g0[1]], "o": [[round(float(a), 1), round(float(b), 2)] for a, b in zip(s, np.nan_to_num(v))]}
    return out


def move_lines(G: nx.DiGraph, offsets: dict, smooth_m: float = 90.0) -> dict:
    """Move every vertex of each road along its normal by the smoothed offset; junctions stay. In place.

    Only the drawing moves: ``length_m`` (and so every route time) is kept. Moving the vertices of a pixel line one
    by one adds small wiggles, about 1% of length on Scenario 1, which made routes longer than BD TOPO's; the
    displacement is a question of where the road is, not of how long it is."""
    import pyproj

    from .scanlearn import _key

    tr = pyproj.Transformer.from_crs("EPSG:4326", L93, always_xy=True)
    inv = pyproj.Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    moved, km, total = 0, 0.0, 0.0
    done = {}
    for u, v, d in G.edges(data=True):
        k = _key(G, u, v)
        if k not in offsets or not d.get("coords"):
            continue
        if k in done:  # the reverse direction: same geometry reversed
            c, L = done[k]
            here = (G.nodes[u]["x"], G.nodes[u]["y"])
            d["coords"] = c if np.allclose(c[0], here, atol=1e-7) else tuple(reversed(c))
            continue
        xy = _xy(tr, d["coords"])
        seg = np.hypot(*np.diff(xy, axis=0).T)
        ch = np.concatenate([[0.0], np.cumsum(seg)])
        o = np.asarray(offsets[k]["o"], float)
        # offsets were computed along the road table's geometry, which starts at offsets[k]["start"]
        first = np.asarray(d["coords"][0])
        s_off, val = o[:, 0], o[:, 1]
        flip = not np.allclose(first, np.asarray(offsets[k]["start"]), atol=1e-7)
        if flip:  # this direction runs the other way: chainage and left side are mirrored
            s_off, val = ch[-1] - s_off, -val
            order = np.argsort(s_off)
            s_off, val = s_off[order], val[order]
        if len(s_off) >= 3:
            w = max(1, int(round(smooth_m / STEP_M)))
            ker = np.ones(w) / w
            val = np.convolve(np.pad(val, (w // 2, w - 1 - w // 2), mode="edge"), ker, mode="valid")
        off = np.interp(ch, s_off, val, left=0.0, right=0.0)
        taper = np.clip(np.minimum(ch, ch[-1] - ch) / END_M, 0.0, 1.0)
        off = np.clip(off * taper, -MAX_M, MAX_M)
        t, n = _normals(xy, ch, ch, h=15.0)
        nxy = xy + off[:, None] * n
        lon, lat = inv.transform(nxy[:, 0], nxy[:, 1])
        c = tuple(zip(lon.tolist(), lat.tolist()))
        c = (tuple(d["coords"][0]),) + c[1:-1] + (tuple(d["coords"][-1]),)
        d["coords"] = c
        done[k] = (c, d["length_m"])
        moved += 1
        km += d["length_m"] / 1000.0
        total += float(np.abs(off).mean())
    return {"roads moved": moved, "km moved": round(km), "mean shift m": round(total / max(moved, 1), 1)}


def save(out: Path, model, offsets: dict, sc: dict) -> None:
    model.booster_.save_model(str(Path(out) / "model_geometry.txt"))
    (Path(out) / "geometry_oof_offsets.json").write_text(json.dumps(offsets), encoding="utf-8")
    (Path(out) / "geometry_scores.json").write_text(json.dumps(sc, indent=1), encoding="utf-8")


def predict_offsets(G: nx.DiGraph, tif: str, model_path) -> dict:
    """Offsets of every road of a sheet the model was not trained on (no shapefile needed)."""
    import lightgbm as lgb

    from .scanlearn import class_raster, road_table

    classes, transform = class_raster(tif)
    roads = road_table(G)
    X, where = samples(G, roads, classes, transform)
    pred = lgb.Booster(model_file=str(model_path)).predict(X)
    return offsets_by_road(roads, where, pred)


def learn(G: nx.DiGraph, roads, tif: str, bdtopo: str, bounds: tuple, grid: int, folds_of_blocks, progress=print):
    """Samples, labels, out-of-fold model and scores; also the network-level gate: on unseen blocks, the moved
    network must lie closer to BD TOPO (share within 25 m) than the original."""
    import copy

    import pyproj

    from .realbench import BDScorer
    from .scanlearn import _key, blocks_of, class_raster

    classes, transform = class_raster(tif)
    X, where = samples(G, roads, classes, transform)
    y = labels(where, bdtopo)
    inv = pyproj.Transformer.from_crs(L93, "EPSG:4326", always_xy=True)
    P = np.vstack([w[2] for w in where])
    lon, lat = inv.transform(P[:, 0], P[:, 1])
    blk = blocks_of(np.column_stack([lon, lat]), bounds, grid)
    oof, model, sc = train_and_score(X, y, blk, folds_of_blocks, progress=progress)
    offsets = offsets_by_road(roads, where, oof)
    scorer = BDScorer(bdtopo)

    def net(Gx):
        seen, lines = set(), []
        for u, v, d in Gx.edges(data=True):
            k = _key(Gx, u, v)
            if k not in seen and d.get("coords"):
                seen.add(k)
                lines.append(d["coords"])
        return scorer.score(lines)

    before = net(G)
    G2 = copy.deepcopy(G)
    moved = move_lines(G2, offsets)
    after = net(G2)
    sc.update({"network_25m_before": before["bd_25m"], "network_25m_after": after["bd_25m"], "network_50m_before": before["bd_50m"],
               "network_50m_after": after["bd_50m"], **moved})
    sc["enabled"] = bool(after["bd_25m"] > before["bd_25m"] and sc["mean_after_m"] < sc["mean_offset_m"])
    progress(f"  moved network within 25 m of BD TOPO: {before['bd_25m']:.1%} -> {after['bd_25m']:.1%} -> "
             f"{'on' if sc['enabled'] else 'off'}")
    return model, offsets, sc

"""Calibrate the U-Net road score into a probability, out of fold, and report its reliability.

For each fold, an isotonic (monotone) mapping from the 256 score levels to the share of BD TOPO road pixels is fitted on
the pixels of the three other folds and applied to this fold. The calibrated probability is "the chance that the pixel
is in the road label band" (BD TOPO centre lines drawn 3 px wide, as in training). Only the road channel changes; the
order of the scores is kept, so the traced network can be kept as it is (the folder reuses the raw network.pkl).

    python scripts/calibrate.py --prc outputs/x/prc --run mix12k --network outputs/x/retrace/base/network.pkl --out outputs/x/prc/mix12k_cal
    python scripts/calibrate.py --prc outputs/x/prc --run mix12k --reliability-only      # the table only
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np

from coa_net.scanunet import load_raster, save_raster


def level_stats(p8: np.ndarray, y: np.ndarray, m: np.ndarray):
    """Pixel count and road count for each of the 256 score levels, on the pixels of mask m."""
    lv = p8[m].astype(np.int64)
    return np.bincount(lv, minlength=256).astype(float), np.bincount(lv, weights=y[m].astype(float), minlength=256)


def isotonic_lut(n, k) -> np.ndarray:
    from sklearn.isotonic import IsotonicRegression

    x = np.arange(256)
    w = n > 0
    ir = IsotonicRegression(y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip")
    ir.fit(x[w], k[w] / n[w], sample_weight=n[w])
    return ir.predict(x)


def reliability(p: np.ndarray, y: np.ndarray, bins=10) -> tuple[list[dict], float]:
    """p in [0, 1] (flattened sample), y bool."""
    rows, ece = [], 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        m = (p >= lo) & (p < hi if i < bins - 1 else p <= hi)
        if m.any():
            rows.append({"bin": f"{lo:.1f}-{hi:.1f}", "share_pixels": float(m.mean()), "mean_p": float(p[m].mean()), "road_rate": float(y[m].mean())})
            ece += m.mean() * abs(y[m].mean() - p[m].mean())
    return rows, float(ece)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prc", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--network", help="network.pkl to reuse (the raw tracing)")
    ap.add_argument("--out")
    ap.add_argument("--reliability-only", action="store_true")
    a = ap.parse_args()
    prc = Path(a.prc)
    P, tr = load_raster(prc / a.run / "unet_oof.tif")
    y = np.load(prc / "road.npy") > 0
    fold = np.load(prc / "fold.npy")
    rng = np.random.default_rng(0)
    idx = rng.integers(0, y.size, 20_000_000)
    raw = P[..., 0].ravel()[idx] / 255.0
    rel_raw, ece_raw = reliability(raw, y.ravel()[idx])
    res = {"run": a.run, "raw": {"reliability": rel_raw, "ece": ece_raw}}
    if not a.reliability_only:
        p8 = P[..., 0]
        cal = np.zeros(p8.shape, np.float32)
        luts = {}
        for f in range(int(fold.max()) + 1):
            n, k = level_stats(p8, y, fold != f)
            lut = isotonic_lut(n, k)
            luts[f] = lut
            m = fold == f
            cal[m] = lut[p8[m]]
        Pc = P.copy()
        Pc[..., 0] = np.clip(cal * 255 + 0.5, 0, 255).astype(np.uint8)
        rel_cal, ece_cal = reliability(cal.ravel()[idx], y.ravel()[idx])
        res["calibrated"] = {"reliability": rel_cal, "ece": ece_cal}
        # what the raw tracing threshold 0.5 (level 128) means on the calibrated scale, per fold
        res["raw_0.5_on_calibrated_scale"] = {str(f): round(float(l[128]), 3) for f, l in luts.items()}
        res["luts"] = {str(f): [round(float(v), 4) for v in l] for f, l in luts.items()}
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        import rasterio

        with rasterio.open(prc / a.run / "unet_oof.tif") as src:
            crs = src.crs
        save_raster(out / "unet_oof.tif", Pc, tr, crs)
        if a.network:
            shutil.copy(a.network, out / "network.pkl")
        (out / "calibration.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
        # for scripts that read prc/<run>/unet_scores.json
        sc = prc / a.run / "unet_scores.json"
        if sc.exists():
            shutil.copy(sc, out / "unet_scores.json")
    print(json.dumps({k: v for k, v in res.items() if k != "luts"}, indent=1))
    if a.out:
        (Path(a.out) / "reliability.json").write_text(json.dumps({k: v for k, v in res.items() if k != "luts"}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

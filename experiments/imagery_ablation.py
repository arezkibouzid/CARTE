"""Transfer learning and input imagery ablation of the U-Net (GPU): how fine-tuning and the type / quality of the input
image change the road prediction. Illustrates what adapting the model to other imagery (civilian maps, other scales,
scans of lower quality) would involve.

Models (all the same PRC Attention Res-U-Net, Mühlematter et al. 2024):
  zeroshot          the published weights (Swiss Siegfried maps), no fine-tuning on IGN maps
  finetuned_hack    fine-tuned 6k steps on the Hackathon sheet only (4 folds, out of fold)
  mix12k_nodice     fine-tuned 12k steps on the Hackathon sheet + a second IGN sheet, BCE without Dice (the best model)

Input imagery (applied to the whole sheet, then the held-out fold is predicted by its fold model):
  original, grayscale, blur (sigma 1.5 px), half resolution (20 m/px, re-sampled to the grid), quarter resolution
  (40 m/px), JPEG quality 20, Gaussian noise (sigma 12), other palette (colour channels permuted: another map style),
  low contrast (x 0.5 around mid-grey)

Scores per (model, imagery), pooled over the 4 held-out folds (each pixel predicted by a model that never saw it):
road AUC, IoU, precision, recall and F1 at p >= 0.5 against the BD TOPO road raster, prediction time and GPU energy.

    sbatch experiments/run_gpu.sh experiments/imagery_ablation.py [--folds 0 1 2 3] [--imagery ...] [--models ...]
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time

import numpy as np

from common import REPO, Run, file_ref

sys.path.insert(0, str(REPO / "scripts"))
PRC = REPO / "outputs" / "hackathon100k" / "prc"
PRC_REPO = str(REPO.parent.parent / "ext" / "ProbRoadClass-DeepLearning")
WEIGHTS = str(REPO.parent.parent / "ext" / "prc_weights" / "prc_binary.pt")


def degrade(rgb: np.ndarray, kind: str, rng) -> np.ndarray:
    from PIL import Image
    from scipy import ndimage

    if kind == "original":
        return rgb
    if kind == "grayscale":
        g = (0.299 * rgb[..., 0] + 0.587 * rgb[..., 1] + 0.114 * rgb[..., 2]).astype(np.uint8)
        return np.repeat(g[..., None], 3, axis=2)
    if kind == "blur":
        return np.stack([ndimage.gaussian_filter(rgb[..., c], 1.5) for c in range(3)], axis=2)
    if kind in ("half_resolution", "quarter_resolution"):
        f = 2 if kind == "half_resolution" else 4
        im = Image.fromarray(rgb)
        small = im.resize((rgb.shape[1] // f, rgb.shape[0] // f), Image.BILINEAR)
        return np.asarray(small.resize((rgb.shape[1], rgb.shape[0]), Image.BILINEAR))
    if kind == "jpeg_q20":
        out = np.empty_like(rgb)
        step = 4096  # JPEG is limited to 65535 px a side: encode in strips
        for r0 in range(0, rgb.shape[0], step):
            b = io.BytesIO()
            Image.fromarray(rgb[r0:r0 + step]).save(b, "JPEG", quality=20)
            out[r0:r0 + step] = np.asarray(Image.open(io.BytesIO(b.getvalue())).convert("RGB"))
        return out
    if kind == "noise_12":
        out = np.empty_like(rgb)
        for r0 in range(0, rgb.shape[0], 1024):
            blk = rgb[r0:r0 + 1024].astype(np.int16) + rng.normal(0, 12, rgb[r0:r0 + 1024].shape).astype(np.int16)
            out[r0:r0 + 1024] = np.clip(blk, 0, 255).astype(np.uint8)
        return out
    if kind == "other_palette":
        return np.ascontiguousarray(rgb[..., [2, 0, 1]])
    if kind == "low_contrast":
        return np.clip((rgb.astype(np.float32) - 128) * 0.5 + 128, 0, 255).astype(np.uint8)
    raise ValueError(kind)


IMAGERY = ["original", "grayscale", "blur", "half_resolution", "quarter_resolution", "jpeg_q20", "noise_12", "other_palette", "low_contrast"]
MODELS = ["zeroshot", "finetuned_hack", "mix12k_nodice"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folds", nargs="*", type=int, default=[0, 1, 2, 3])
    ap.add_argument("--imagery", nargs="*", default=IMAGERY)
    ap.add_argument("--models", nargs="*", default=MODELS)
    a = ap.parse_args()
    import torch
    from sklearn.metrics import roc_auc_score

    import prc_unet as U

    ckpt = {"zeroshot": None, "finetuned_hack": "prc_finetuned_fold{f}.pt", "mix12k_nodice": "prc_mix12k_nodice_fold{f}.pt"}
    cfg = {"script": "experiments/imagery_ablation.py", "folds": a.folds, "imagery": a.imagery, "models": a.models,
           "checkpoints": {m: ([file_ref(PRC / ckpt[m].format(f=f)) for f in a.folds] if ckpt[m] else [file_ref(WEIGHTS)]) for m in a.models},
           "data": [file_ref(PRC / f"{k}.npy") for k in ("rgb", "road", "fold")], "threshold": 0.5, "tile": U.TILE, "margin": U.MARGIN,
           "auc_sample_per_fold": 1_000_000}
    with Run("imagery_ablation", cfg, gpu=True) as R:
        d = U.load(PRC, mmap=False)
        rgb0, road, fold = d["rgb"], d["road"], d["fold"]
        rng = np.random.default_rng(0)
        nets = {}

        def net_for(m, f):
            key = (m, None if m == "zeroshot" else f)
            if key not in nets:
                nets.clear()
                n = U.build(PRC_REPO, WEIGHTS)
                if m != "zeroshot":
                    n.load_state_dict(torch.load(PRC / ckpt[m].format(f=f), map_location="cpu"))
                nets[key] = n.cuda().eval()
            return nets[key]

        for kind in a.imagery:
            with R.phase(f"degrade {kind}"):
                rgb = degrade(rgb0, kind, rng)
            for m in a.models:
                tp = fp = fn = 0
                ys, ps = [], []
                sec = 0.0
                for f in a.folds:
                    where = fold == f
                    net = net_for(m, f)
                    torch.cuda.synchronize()
                    t = time.perf_counter()
                    with R.phase(f"predict {m}"):
                        P = U.predict(net, rgb, where=where)
                        torch.cuda.synchronize()
                    sec += time.perf_counter() - t
                    pred, y = P[..., 0][where] >= 128, road[where] > 0
                    tp += int((pred & y).sum())
                    fp += int((pred & ~y).sum())
                    fn += int((~pred & y).sum())
                    idx = np.flatnonzero(where.ravel())
                    sub = idx[np.random.default_rng(f).integers(0, len(idx), min(1_000_000, len(idx)))]
                    ys.append(road.ravel()[sub] > 0)
                    ps.append(P[..., 0].ravel()[sub] / 255.0)
                    del P
                yy, pp = np.concatenate(ys), np.concatenate(ps)
                prec, rec_ = tp / max(tp + fp, 1), tp / max(tp + fn, 1)
                rec = {"imagery": kind, "model": m, "folds": len(a.folds), "road_auc": round(float(roc_auc_score(yy, pp)), 4),
                       "road_iou": round(tp / max(tp + fp + fn, 1), 4), "precision": round(prec, 4), "recall": round(rec_, 4),
                       "f1": round(2 * prec * rec_ / max(prec + rec_, 1e-9), 4), "predict_s": round(sec, 2),
                       "megapixels": round(float((fold >= 0).sum() if len(a.folds) == 4 else sum((fold == f).sum() for f in a.folds)) / 1e6, 1)}
                R.record(rec)
                R.log(json.dumps(rec))
                R.save()
        # summary: drop of each imagery relative to the original, per model
        base = {r["model"]: r for r in R.records if r["imagery"] == "original"}
        R.summary = {f"{r['model']} / {r['imagery']}": {"f1": r["f1"], "iou": r["road_iou"], "auc": r["road_auc"],
                                                        "f1_change_vs_original": round(r["f1"] - base[r["model"]]["f1"], 4) if r["model"] in base else None}
                     for r in R.records}
        R.save()
        plot(R)


def plot(R):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kinds = list(dict.fromkeys(r["imagery"] for r in R.records))
    models = list(dict.fromkeys(r["model"] for r in R.records))
    fig, ax = plt.subplots(figsize=(11, 4.5))
    w = 0.8 / len(models)
    for i, m in enumerate(models):
        v = [next((r["f1"] for r in R.records if r["model"] == m and r["imagery"] == k), 0) for k in kinds]
        ax.bar(np.arange(len(kinds)) + i * w, v, w, label=m)
    ax.set_xticks(np.arange(len(kinds)) + w * (len(models) - 1) / 2)
    ax.set_xticklabels(kinds, rotation=25, ha="right")
    ax.set_ylabel("F1 route (p >= 0.5), hors pli")
    ax.set_title("U-Net : apprentissage par transfert et type d'image")
    ax.legend()
    ax.grid(axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(R.dir / "imagery_f1.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    main()

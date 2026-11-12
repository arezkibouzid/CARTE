"""ProbRoadClass Attention Res-U-Net (Mühlematter et al. 2024, github.com/DominikM198/ProbRoadClass-DeepLearning)
on an IGN scan: zero-shot, and fine-tuned out of fold on the blocks of `coa-net scan-learn` with BD TOPO as labels.

Writes `unet_oof.tif` in the format of `coa-net scan-unet` (7 bands: p road, p importance 1..5, p structure, x 255),
so it can be used by `scan-learn --unet DIR` and by `coa_net.scanunet.network_from_unet` ("U-Net alone").

    python scripts/prc_unet.py prep      --data examples --out OUT
    python scripts/prc_unet.py zeroshot  --out OUT --weights prc_binary.pt --prc-repo ProbRoadClass-DeepLearning   (GPU)
    python scripts/prc_unet.py finetune  --out OUT --weights prc_binary.pt --prc-repo ... --fold F               (GPU)
    python scripts/prc_unet.py merge     --out OUT --name finetuned
    python scripts/prc_unet.py finetune  ... --fold -1 --tag T          (every block: the model for sheets without labels)
    python scripts/prc_unet.py infer     --tif SHEET.tif --model OUT/prc_T_final.pt --prc-repo ... --dest DIR   (GPU, no labels)

``--tag`` names a fine-tuning run (``--steps``, ``--lr``, ``--batch``), so several runs can share OUT.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

TILE, MARGIN = 512, 64


# --------------------------------------------------------------------------- data

def prep(a):
    from coa_net.scanlearn import fold_of_block
    from coa_net.scanunet import block_raster, label_rasters, scan_rgb
    from coa_net.scenario import ScenarioPaths

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = ScenarioPaths.from_dir(a.data)
    t = time.perf_counter()
    rgb, transform, bounds, crs = scan_rgb(paths.scan100)
    road, imp, struct = label_rasters(paths.bdtopo, rgb.shape[:2], transform)
    fob = fold_of_block(a.grid * a.grid, 4, 0)
    fold = fob[block_raster(rgb.shape[:2], transform, bounds, a.grid)].astype(np.int8)
    for k, v in dict(rgb=rgb, road=road, imp=imp, struct=struct, fold=fold).items():
        np.save(out / f"{k}.npy", v)
    (out / "meta.json").write_text(json.dumps({"tif": paths.scan100, "transform": list(transform)[:6], "crs": str(crs),
                                               "folds_of_blocks": fob.tolist(), "grid": a.grid}), encoding="utf-8")
    print(f"prep: {rgb.shape}, road pixels {road.mean():.1%}, {time.perf_counter() - t:.0f} s")


def load(out: Path, mmap=True):
    m = "r" if mmap else None
    return {k: np.load(out / f"{k}.npy", mmap_mode=m) for k in ("rgb", "road", "imp", "struct", "fold")}


# --------------------------------------------------------------------------- model

def build(prc_repo: str, weights: str):
    import torch
    import torch.nn as nn

    sys.path.insert(0, str(Path(prc_repo) / "01_CNN"))
    from models.models import Resnet18_Unet_Big_Attention

    base = Resnet18_Unet_Big_Attention(1, imagenet1k=False, p=0.1)
    sd = torch.load(weights, map_location="cpu", weights_only=False)
    sd = sd.get("model_state_dict", sd.get("state_dict", sd)) if isinstance(sd, dict) else sd.state_dict()
    missing, unexpected = base.load_state_dict(sd, strict=False)
    print(f"PRC weights: {len(sd)} tensors, missing {len(missing)}, unexpected {len(unexpected)}", flush=True)
    assert len(missing) == 0, missing[:10]

    class PRC7(nn.Module):
        """PRC road output (pretrained head) + a new head for importance 1..5 and structure on the same features."""

        def __init__(self):
            super().__init__()
            self.base = base
            self.head6 = nn.Conv2d(32, 6, 1)
            self._f = None
            base.final1.register_forward_hook(lambda m, i, o: setattr(self, "_f", o))

        def forward(self, x):
            road = self.base(x)
            o6 = self.head6(self._f)
            return torch.cat([road, o6[:, :5], o6[:, 5:]], 1)

    return PRC7()


def loss_fn(out, road, imp, struct, mask):
    import torch

    from coa_net.scanunet import _loss

    m = mask.float()
    p = torch.sigmoid(out[:, 0]) * m
    y = road.float() * m
    dice = 1 - (2 * (p * y).sum() + 1) / (p.sum() + y.sum() + 1)
    with torch.autocast("cuda", enabled=False):
        base = _loss(out.float(), road, imp, struct, mask)
    return base + dice


def fold_scores(P, road, imp, m, n=2_000_000, seed=0) -> dict:
    """Pixel scores of one held-out fold: road AUC (sample), road IoU, importance accuracy on road pixels."""
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(seed)
    idx = np.flatnonzero(m.ravel())
    sub = idx[rng.integers(0, len(idx), min(n, len(idx)))]
    pr, yr = P[..., 0].ravel()[sub] / 255.0, road.ravel()[sub]
    pred, y = P[..., 0][m] >= 128, road[m] > 0
    iv = imp[m]
    k = iv > 0
    acc = float((np.argmax(P[..., 1:6][m][k], axis=1) + 1 == iv[k]).mean()) if k.any() else float("nan")
    return {"road_auc": round(float(roc_auc_score(yr, pr)), 4), "road_iou": round(float((pred & y).sum() / max((pred | y).sum(), 1)), 4),
            "importance_accuracy": round(acc, 4), "road_pixels_pred": int(pred.sum()), "road_pixels_true": int(y.sum())}


def predict(net, rgb, where=None, batch=16):
    """(H, W, 7) uint8, tiles of 512 with a 64 px margin."""
    import torch

    H, W = rgb.shape[:2]
    out = np.zeros((H, W, 7), np.uint8)
    step = TILE - 2 * MARGIN
    jobs = []
    for r0 in range(0, H, step):
        for c0 in range(0, W, step):
            r1, c1 = min(r0 + step, H), min(c0 + step, W)
            if where is None or where[r0:r1, c0:c1].any():
                jobs.append((r0, c0, r1, c1))
    pad = np.pad(rgb, ((MARGIN, TILE), (MARGIN, TILE), (0, 0)), mode="edge")
    net.eval()
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.bfloat16):
        for i in range(0, len(jobs), batch):
            jb = jobs[i:i + batch]
            x = np.stack([pad[r0:r0 + TILE, c0:c0 + TILE] for r0, c0, _, _ in jb])
            x = torch.from_numpy(x).cuda().permute(0, 3, 1, 2).float() / 255.0
            o = net(x).float()
            p = torch.cat([torch.sigmoid(o[:, :1]), torch.softmax(o[:, 1:6], 1), torch.sigmoid(o[:, 6:])], 1)
            p = (p.permute(0, 2, 3, 1).cpu().numpy() * 255 + 0.5).astype(np.uint8)
            for (r0, c0, r1, c1), q in zip(jb, p):
                t = q[MARGIN:MARGIN + r1 - r0, MARGIN:MARGIN + c1 - c0]
                if where is None:
                    out[r0:r1, c0:c1] = t
                else:
                    w = where[r0:r1, c0:c1]
                    out[r0:r1, c0:c1][w] = t[w]
    return out


# --------------------------------------------------------------------------- commands

def zeroshot(a):
    import torch

    out = Path(a.out)
    d = load(out)
    net = build(a.prc_repo, a.weights).cuda()
    t = time.perf_counter()
    P = predict(net, np.asarray(d["rgb"]))
    # the new head is untrained: importance "5" everywhere (rank only filters roads; speeds come from the highway tag),
    # no structure
    P[..., 1:5] = 0
    P[..., 5] = 255
    P[..., 6] = 0
    np.save(out / "pred_zeroshot.npy", P)
    print(f"zero-shot prediction {time.perf_counter() - t:.0f} s, road pixels >= 0.5: {(P[..., 0] >= 128).mean():.1%}")


def finetune(a):
    import torch

    out = Path(a.out)
    d = load(out, mmap=False)
    rgb, road, imp, struct, fold = (d[k] for k in ("rgb", "road", "imp", "struct", "fold"))
    f = a.fold
    train_mask = fold != f  # f = -1: every block (final model, nothing held out)
    seed = f if f >= 0 else 99  # final model (fold -1): its own seed
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = build(a.prc_repo, a.weights).cuda()
    opt = torch.optim.AdamW([{"params": net.base.parameters(), "lr": a.lr},
                             {"params": net.head6.parameters(), "lr": a.lr * 10}], weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=[a.lr, a.lr * 10], total_steps=a.steps, pct_start=0.1)
    H, W = road.shape
    rr, cc = np.nonzero(train_mask[TILE // 2:H - TILE // 2:32, TILE // 2:W - TILE // 2:32])
    corners = np.column_stack([rr * 32, cc * 32])
    t0 = time.perf_counter()
    net.train()
    for step in range(a.steps):
        pick = corners[rng.integers(0, len(corners), a.batch)]
        sl = [(slice(r0, r0 + TILE), slice(c0, c0 + TILE)) for r0, c0 in pick]
        x = torch.from_numpy(np.stack([rgb[s] for s in sl])).cuda().permute(0, 3, 1, 2).float() / 255.0
        ys = [torch.from_numpy(np.stack([arr[s] for s in sl])).cuda() for arr in (road, imp, struct, train_mask)]
        k = int(rng.integers(8))
        aug = lambda t: (torch.flip(torch.rot90(t, k % 4, (-2, -1)), (-1,)) if k >= 4 else torch.rot90(t, k % 4, (-2, -1)))
        x, ys = aug(x), [aug(y) for y in ys]
        opt.zero_grad(set_to_none=True)
        with torch.autocast("cuda", dtype=torch.bfloat16):
            o = net(x)
        loss = loss_fn(o, *ys)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
        opt.step()
        sched.step()
        if (step + 1) % 250 == 0:
            print(f"fold {f} step {step + 1}/{a.steps}: loss {loss.item():.3f}, {time.perf_counter() - t0:.0f} s", flush=True)
        if f >= 0 and a.eval_every and (step + 1) % a.eval_every == 0:
            P = predict(net, rgb, where=fold == f)
            sc = fold_scores(P, road, imp, fold == f)
            sc.update(tag=a.tag, fold=f, step=step + 1, seconds=round(time.perf_counter() - t0))
            with open(out / f"checkpoints_{a.tag}.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(sc) + "\n")
            print(f"fold {f} checkpoint {step + 1}: {sc}", flush=True)
            if step + 1 == a.steps:
                np.save(out / f"pred_{a.tag}_fold{f}.npy", P)
            net.train()
    name = "final" if f < 0 else f"fold{f}"
    torch.save(net.state_dict(), out / f"prc_{a.tag}_{name}.pt")
    (out / f"prc_{a.tag}_config.json").write_text(json.dumps({"steps": a.steps, "lr": a.lr, "batch": a.batch, "tile": TILE,
                                                               "dropout": 0.1, "weights": a.weights}, indent=1), encoding="utf-8")
    if f >= 0 and not (out / f"pred_{a.tag}_fold{f}.npy").exists():
        P = predict(net, rgb, where=fold == f)
        np.save(out / f"pred_{a.tag}_fold{f}.npy", P)
    print(f"{name} done in {time.perf_counter() - t0:.0f} s", flush=True)


def infer(a):
    """A fine-tuned model on any sheet, without labels: unet_oof.tif-format raster (for --network prc-alone)."""
    import torch

    from coa_net.scanunet import save_raster, scan_rgb

    rgb, transform, _, crs = scan_rgb(a.tif)
    net = build(a.prc_repo, a.weights).cuda()
    net.load_state_dict(torch.load(a.model, map_location="cuda"))
    t = time.perf_counter()
    P = predict(net, rgb)
    dest = Path(a.dest)
    dest.mkdir(parents=True, exist_ok=True)
    save_raster(dest / "unet_oof.tif", P, transform, crs)
    (dest / "inference.json").write_text(json.dumps({"tif": str(a.tif), "model": str(a.model), "note": "inference without labels: "
                                                     "not out of fold if the model was trained on this sheet"}, indent=1), encoding="utf-8")
    print(f"inference on {a.tif}: {time.perf_counter() - t:.0f} s, road pixels >= 0.5: {(P[..., 0] >= 128).mean():.1%}")


def merge(a):
    import rasterio
    from affine import Affine

    from coa_net.scanunet import save_raster, scores

    out = Path(a.out)
    d = load(out)
    meta = json.loads((out / "meta.json").read_text(encoding="utf-8"))
    if (out / f"pred_{a.name}_fold0.npy").exists():
        P = np.zeros(d["road"].shape + (7,), np.uint8)
        fold = np.asarray(d["fold"])
        for f in range(4):
            Pf = np.load(out / f"pred_{a.name}_fold{f}.npy", mmap_mode="r")
            m = fold == f
            P[m] = Pf[m]
    else:
        P = np.load(out / f"pred_{a.name}.npy")
    dest = out / a.name
    dest.mkdir(exist_ok=True)
    save_raster(dest / "unet_oof.tif", P, Affine(*meta["transform"]), rasterio.crs.CRS.from_string(meta["crs"]))
    sc = scores(P, np.asarray(d["road"]), np.asarray(d["imp"]), np.asarray(d["struct"]))
    (dest / "unet_scores.json").write_text(json.dumps(sc, indent=1), encoding="utf-8")
    print(a.name, json.dumps(sc))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("prep", "zeroshot", "finetune", "merge", "infer"))
    ap.add_argument("--data")
    ap.add_argument("--out", required=True)
    ap.add_argument("--grid", type=int, default=4)
    ap.add_argument("--prc-repo")
    ap.add_argument("--weights")
    ap.add_argument("--fold", type=int)
    ap.add_argument("--steps", type=int, default=6000)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--name", default="finetuned")
    ap.add_argument("--eval-every", type=int, default=0, help="finetune: score the held-out fold every N steps (checkpoints_TAG.jsonl)")
    ap.add_argument("--tag", default="finetuned", help="name of the fine-tuning run")
    ap.add_argument("--tif", help="infer: the sheet")
    ap.add_argument("--model", help="infer: a fine-tuned model (.pt)")
    ap.add_argument("--dest", help="infer: output folder")
    a = ap.parse_args()
    {"prep": prep, "zeroshot": zeroshot, "finetune": finetune, "merge": merge, "infer": infer}[a.cmd](a)


if __name__ == "__main__":
    main()

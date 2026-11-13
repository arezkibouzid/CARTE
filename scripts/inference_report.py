"""Report: hardware, training time of the best run, inference methods (time and quality), CPU pipeline time.

    python scripts/inference_report.py --root outputs/hackathon100k --run mix12k --doc docs/prc_inference.md
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

METHODS = ("base", "tta", "overlap", "tta_overlap")
LABEL = {"base": "base: 512 px tiles, central 384 px kept, no overlap", "tta": "TTA: 8 rotations / mirror images averaged",
         "overlap": "overlap: tiles every 256 px, smooth blending", "tta_overlap": "TTA + overlap"}


def jload(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def train_log(p: Path) -> dict:
    if not p.exists():
        return {}
    t = p.read_text(errors="ignore")
    d = {}
    m = re.search(r"^(NVIDIA [^,\n]+), (\d+) MiB", t, re.M)
    if m:
        d["gpu"], d["gpu_mem_gb"] = m.group(1), round(int(m.group(2)) / 1024)
    m = re.search(r"node (\S+), driver CUDA (\d+), python (\S+)", t)
    if m:
        d["node"], d["cuda_driver"], d["venv"] = m.group(1), m.group(2), m.group(3)
    steps = re.findall(r"step (\d+)/(\d+): loss ([0-9.]+), (\d+) s", t)
    if steps:
        s, tot, loss, sec = steps[-1]
        d.update(steps=int(tot), train_seconds=int(sec), final_loss=float(loss))
    m = re.search(r"(fold\d|final) done in (\d+) s", t)
    if m:
        d["total_seconds"] = int(m.group(2))
    d["checkpoints"] = len(re.findall(r"checkpoint \d+:", t))
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--doc", required=True)
    a = ap.parse_args()
    root = Path(a.root)
    prc = root / "prc"
    run = a.run
    cfg = jload(prc / f"prc_{run}_config.json") or {}
    L = [f"# PRC {run}: training and inference time, hardware, and inference methods", "",
         "*Sheet `Hackathon_100K.tif` (21,019 x 9,890 px = 207.9 megapixels). Every quality figure is out of fold (each block predicted "
         "by the model of the fold that held it out) and measured on the `examples` data only.*", ""]

    # hardware
    t0 = jload(next(iter(sorted(prc.glob(f"timing_{run}_base_fold*.json"))), Path("/nonexistent")))
    pt = jload(root / "pipeline_timing" / "pipeline_timing.json")
    L += ["## 1. Hardware and software", "", "| part | used |", "| --- | --- |"]
    if t0:
        L.append(f"| GPU (training, inference) | {t0.get('gpu')} ({t0.get('gpu_memory_gb')} GB), one per fold; node {t0.get('node')}; "
                 f"PyTorch {t0.get('torch')} (CUDA {t0.get('cuda')}), bfloat16 autocast |")
    if pt:
        h = pt["hardware"]
        L.append(f"| CPU (network tracing, routing, exercises) | {h['cpu']}, {h['cores_allocated']} cores allocated (Slurm), node {h['node']}; "
                 f"Python {h['python']} |")
    L += ["| cluster | 2 nodes x 8 NVIDIA H100 80 GB (Slurm, partition `low`); at most 2 GPUs per configuration |", ""]

    # training
    L += [f"## 2. Training of the best run ({run}: {cfg.get('steps', '?'):,} steps, batch {cfg.get('batch')}, learning rate {cfg.get('lr')}, "
          f"tiles {cfg.get('tile')} px, extra sheets {', '.join(cfg.get('extra') or []) or 'none'} at {cfg.get('extra_share', 0):.0%} of the tiles)", "",
          "| model | node | GPU | steps | training time | incl. held-out scoring and prediction | steps/s | tiles/s | final loss |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    tot = 0
    for name, log in [(f"fold {f}", prc / "logs" / f"ft_{run}_{f}.log") for f in range(4)] + [("final (every block)", prc / "logs" / f"final_{run}.log")]:
        d = train_log(log)
        if not d:
            continue
        tot += d.get("total_seconds", 0)
        st = d.get("steps", 0) / max(d.get("train_seconds", 1), 1)
        L.append(f"| {name} | {d.get('node', '-')} | {d.get('gpu', '-')} | {d.get('steps', 0):,} | {d.get('train_seconds', 0) / 60:.1f} min | "
                 f"{d.get('total_seconds', 0) / 60:.1f} min | {st:.1f} | {st * cfg.get('batch', 16):.0f} | {d.get('final_loss', float('nan')):.3f} |")
    L += ["", f"GPU time for the run: {tot / 3600:.2f} GPU-hours (4 folds + final model). With 2 GPUs per configuration the 4 folds take "
          "2 rounds; the held-out fold is scored every 3,000 steps (`--eval-every`), which is included in the second time column.", ""]

    # inference: out of fold per method
    L += ["## 3. Inference methods (out of fold, the 4 folds summed)", "",
          "| method | GPU time | tiles | forward passes | megapixels/s | road AUC | road IoU | importance |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in METHODS:
        ts = [jload(p) for p in sorted(prc.glob(f"timing_{run}_{m}_fold*.json"))]
        sc = jload(prc / f"{run}_{m}" / "unet_scores.json")
        if not ts:
            continue
        sec = sum(t["seconds"] for t in ts)
        px_ = sum(t["pixels"] for t in ts)
        L.append(f"| {LABEL[m]} | {sec:.0f} s | {sum(t['tiles'] for t in ts):,} | {sum(t['forward_passes'] for t in ts):,} | {px_ / 1e6 / sec:.2f} | "
                 + (f"{sc['road_auc']:.4f} | {sc['road_iou']:.4f} | {sc['importance_accuracy']:.1%} |" if sc else "- | - | - |"))
    L += ["", "Timed with CUDA synchronisation, batch 16, model already loaded (loading the model and the sheet adds about 10-20 s per run).", ""]

    # full-sheet deployment time
    L += ["## 4. Whole sheet, final model (deployment: a sheet without labels)", "",
          "| method | time for the whole sheet | megapixels/s | tiles | forward passes | GPU |", "| --- | --- | --- | --- | --- | --- |"]
    for m in METHODS:
        d = jload(root / f"prc_final_infer_mix_{m}" / "inference.json")
        if d and "seconds" in d:
            L.append(f"| {LABEL[m]} | {d['seconds']:.0f} s | {d['megapixels_per_s']:.2f} | {d.get('tiles', '-')} | {d.get('forward_passes', '-')} | {d.get('gpu', '-')} |")
    L.append("")

    # quality downstream
    abl = jload(root / "ablation_inference" / "ablation.json")
    exa = jload(root / "exercise_ablation_inference" / "exercise_ablation.json")
    L += ["## 5. Effect on the reconstructed network, the routes and the uncertainty", "",
          "| method | network within 25 m of BD TOPO | route score town / random | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM "
          "| gap km on the BD TOPO roads not followed | broken-network km (BD TOPO) | uncertainty vs missed road (Spearman) | missed road in the 20% most uncertain cells | gap points in those cells |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for m in METHODS:
        name = f"{run}_{m}"
        ar = next((r for r in (abl or {}).get("rows", []) if r["variant"].startswith(f"PRC {name} ")), None)
        ex = [r for r in (exa or []) if r.get("network") == f"PRC {name}" and "error" not in r]
        mm = jload(root / f"mismatch_inference_{m}" / "mismatch.json")
        us = jload(root / f"uncertainty_inference_{m}" / "uncertainty_stats.json")
        gap = broken = None
        if mm:
            zs = [z for e in mm for z in e["zones"]]
            gap = sum(z.get("gap km on BD TOPO road", 0) for z in zs)
            broken = sum(z.get("BD TOPO km", 0) for z in zs if z.get("cause vs BD TOPO") == "road seen, broken in the PRC network")
        mean = lambda k: sum(abs(r[k]) if "gap" in k else r[k] for r in ex) / len(ex) if ex else float("nan")
        L.append(f"| {LABEL[m]} | " + (f"{ar['within_25m']:.1%} | {ar['town']['score']:.3f} / {ar['random']['score']:.3f}" if ar else "- | -")
                 + (f" | {mean('BD TOPO length gap'):.1%} / {mean('OSM length gap'):.1%} | {mean('BD TOPO overlap'):.1%} / {mean('OSM overlap'):.1%}" if ex else " | - | -")
                 + (f" | {gap:.1f} | {broken:.1f}" if gap is not None else " | - | -")
                 + (f" | {us['spearman_uncertainty_vs_missed']:.2f} | {us['missed_share_top20_uncertain']:.1%} (x{us['lift_top20']:.1f}) | "
                    f"{us.get('gap_points_in_top20_uncertain_cells', float('nan')):.0%} |" if us else " | - | - | - |"))
    L.append("")

    # CPU pipeline
    if pt:
        L += ["## 6. CPU part of the pipeline (per sheet)", "",
              "| raster | load | trace the road network | routing network | solve the 10 exercises | network sections | network km |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
        for r in pt["rows"]:
            L.append(f"| {r['raster']} | {r['load raster (s)']:.1f} s | {r['trace the road network (s)']:.1f} s | "
                     f"{r['routing network: lines, built-up share, speeds (s)']:.1f} s | {r['solve the 10 exercises (s)']:.1f} s | "
                     f"{r['network_sections']:,} | {r['network_km']:,.0f} |")
        L.append("")
    Path(a.doc).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

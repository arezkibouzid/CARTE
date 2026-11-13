"""Report of the robustness runs (clDice connectivity loss, uncertainty-guided sampling) against their reference run.

    python scripts/robust_report.py --root outputs/hackathon100k --ref mix12k --tags mix12k_cld mix12k_hard mix12k_cld_hard --doc docs/prc_robust_results.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finish_sweep import curves, runs
from inference_report import jload, train_log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--tags", nargs="+", required=True)
    ap.add_argument("--doc", required=True)
    a = ap.parse_args()
    root = Path(a.root)
    prc = root / "prc"
    tags = [a.ref] + a.tags
    desc = {}
    for t in tags:
        c = jload(prc / f"prc_{t}_config.json") or {}
        parts = []
        if c.get("cldice"):
            parts.append(f"clDice x{c['cldice']:g}")
        if c.get("hard_cells"):
            parts.append(f"{c['hard_share']:.0%} of the sheet's tiles from the {1 - c['hard_quantile']:.0%} most uncertain cells")
        desc[t] = ", ".join(parts) or "reference (no change)"
    rs = {r["tag"]: r for r in runs(prc, tags)}
    ref = rs.get(a.ref)
    L = [f"# Robust training of PRC: connectivity loss and uncertainty-guided sampling (against {a.ref})", "",
         f"*Same data and settings as {a.ref} (Hackathon training folds + Scenario 1, 12,000 steps, batch 16, lr 3e-4); one change per run. "
         "Evaluation on the `examples` data only, out of fold, base inference. The uncertain cells come from the out-of-fold uncertainty "
         f"map of {a.ref} (label-free), restricted to each fold's training blocks.*", "",
         "## 1. Pixels (Hackathon sheet, out of fold)", "",
         "| run | change | road AUC | road IoU | importance | 4 vs 5 | structure AUC |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for t in tags:
        r = rs.get(t)
        if not r:
            L.append(f"| {t} | {desc[t]} | missing | | | | |")
            continue
        d = lambda k, f: f"{r[k]:{f}}" + (f" ({r[k] - ref[k]:+.3f})" if ref and t != a.ref and k in ("road_iou", "road_auc") else "")
        L.append(f"| {t} | {desc[t]} | {d('road_auc', '.4f')} | {d('road_iou', '.4f')} | {r['importance_accuracy']:.1%} | "
                 f"{r['importance_4_vs_5_accuracy']:.1%} | {r['structure_auc']:.3f} |")
    L += ["", "## 2. Held-out road IoU during training (mean of the 4 folds)", "", "| run | " + " | ".join(f"{s:,}" for s in (3000, 6000, 9000, 12000)) + " |",
          "| --- |" + " --- |" * 4]
    cv = curves(prc)
    for t in tags:
        row = {c["step"]: c["road_iou"] for c in cv if c["tag"] == t}
        L.append(f"| {t} | " + " | ".join(f"{row[s]:.4f}" if s in row else "-" for s in (3000, 6000, 9000, 12000)) + " |")
    L += ["", "## 3. Training time (one H100 per fold)", "", "| run | fold minutes (4 folds) | steps/s | GPU-hours |", "| --- | --- | --- | --- |"]
    for t in tags:
        ds = [train_log(prc / "logs" / f"ft_{t}_{f}.log") for f in range(4)]
        ds = [d for d in ds if d]
        if ds:
            mins = ", ".join("%.1f" % (d.get("total_seconds", 0) / 60) for d in ds)
            L.append(f"| {t} | {mins} | "
                     f"{sum(d['steps'] / d['train_seconds'] for d in ds) / len(ds):.1f} | {sum(d.get('total_seconds', 0) for d in ds) / 3600:.2f} |")
    abl = jload(root / "ablation_robust" / "ablation.json")
    exa = jload(root / "exercise_ablation_robust" / "exercise_ablation.json")
    L += ["", "## 4. Reconstructed network, routes, exercises, uncertainty", "",
          "| run | network within 25 m of BD TOPO | traced sections | route score town / random | 10 exercises: length gap BD TOPO / OSM | "
          "overlap BD TOPO / OSM | gap km (BD TOPO roads not followed) | broken-corridor km | gap points in the 20% most uncertain cells |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    pt = jload(root / "pipeline_timing_robust" / "pipeline_timing.json") or {"rows": []}
    for t in tags:
        ar = next((r for r in (abl or {}).get("rows", []) if r["variant"].startswith(f"PRC {t} ")), None)
        ex = [r for r in (exa or []) if r.get("network") == f"PRC {t}" and "error" not in r]
        mm = jload(root / f"mismatch_robust_{t}" / "mismatch.json")
        us = jload(root / f"uncertainty_robust_{t}" / "uncertainty_stats.json")
        sec = next((r["network_sections"] for r in pt["rows"] if r["raster"] == t), None)
        gap = broken = None
        if mm:
            zs = [z for e in mm for z in e["zones"]]
            gap = sum(z.get("gap km on BD TOPO road", 0) for z in zs)
            broken = sum(z.get("BD TOPO km", 0) for z in zs if z.get("cause vs BD TOPO") == "road seen, broken in the PRC network")
        mean = lambda k: sum(abs(r[k]) if "gap" in k else r[k] for r in ex) / len(ex)
        cells = [t, f"{ar['within_25m']:.1%}" if ar else "-", f"{sec:,}" if sec else "-",
                 f"{ar['town']['score']:.3f} / {ar['random']['score']:.3f}" if ar else "-"]
        if ex:
            solved = "" if len(ex) == 10 else f" ({len(ex)}/10 solved)"
            cells += [f"{mean('BD TOPO length gap'):.1%} / {mean('OSM length gap'):.1%}{solved}",
                      f"{mean('BD TOPO overlap'):.1%} / {mean('OSM overlap'):.1%}"]
        else:
            cells += ["-", "-"]
        cells += [f"{gap:.1f}" if gap is not None else "-", f"{broken:.1f}" if broken is not None else "-",
                  f"{us.get('gap_points_in_top20_uncertain_cells', float('nan')):.0%}" if us else "-"]
        L.append("| " + " | ".join(cells) + " |")
    Path(a.doc).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

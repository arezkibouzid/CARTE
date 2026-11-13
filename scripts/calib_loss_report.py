"""Reports of experiment 1 (calibration of the U-Net score) and experiment 2 (road loss ablation).

    python scripts/calib_loss_report.py calibration --root outputs/hackathon100k --doc docs/prc_calibration.md
    python scripts/calib_loss_report.py loss --root outputs/hackathon100k --runs mix12k mix12k_nodice ... --doc docs/prc_loss_results.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from finish_sweep import runs as pixel_runs
from inference_report import train_log


def jl(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def rel_table(rel: dict, title: str) -> list[str]:
    L = [f"| {title}: score bin | mean value | share that is BD TOPO road | share of pixels |", "| --- | --- | --- | --- |"]
    for r in rel["reliability"]:
        L.append(f"| {r['bin']} | {r['mean_p']:.2f} | {r['road_rate']:.1%} | {r['share_pixels']:.2%} |")
    return L + [f"| expected calibration error | {rel['ece']:.3f} | | |"]


def bridge_rows(root: Path, names: list[tuple[str, str]]) -> list[str]:
    L = ["| bridging | links | km | on a BD TOPO road | at least partly | no road | OSM confirms |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for label, d in names:
        j = jl(root / d / "bridge_check.json")
        md = root / d / "bridge_check.md"
        if not j:
            L.append(f"| {label} | - | | | | | |")
            continue
        no_road = "-"
        if md.exists():
            for line in md.read_text().splitlines():
                if line.startswith("| no road |"):
                    no_road = line.split("|")[4].strip()
        L.append(f"| {label} | {j['links']:,} | {j['km']:.1f} | {j['precision_road']:.0%} | {j['precision_partly']:.0%} | {no_road} | {j['osm_confirms']:.0%} |")
    return L


def route_rows(root: Path, abl: str, exa: str, names: list[tuple[str, str]], gaps: dict | None = None) -> list[str]:
    a = jl(root / abl / "ablation.json") or {"rows": []}
    e = jl(root / exa / "exercise_ablation.json") or []
    head = "| network | route score town / random | town time gap | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM |"
    sep = "| --- | --- | --- | --- | --- | --- |"
    if gaps:
        head += " gap km (BD TOPO roads not followed) | of it in stretches over 300 m |"
        sep += " --- | --- |"
    L = [head, sep]
    for label, name in names:
        ar = next((r for r in a["rows"] if r["variant"].startswith(f"PRC {name} ")), None)
        ex = [r for r in e if r.get("network") == f"PRC {name}" and "error" not in r]
        m = (lambda k: sum(abs(r[k]) if "gap" in k else r[k] for r in ex) / len(ex)) if ex else None
        c = [label, f"{ar['town']['score']:.3f} / {ar['random']['score']:.3f}" if ar else "-", f"{ar['town']['time_gap']:+.1%}" if ar else "-",
             f"{ar['reached']:.0%}" if ar else "-",
             f"{m('BD TOPO length gap'):.1%} / {m('OSM length gap'):.1%}" + ("" if len(ex) == 10 else f" ({len(ex)}/10)") if ex else "-",
             f"{m('BD TOPO overlap'):.1%} / {m('OSM overlap'):.1%}" if ex else "-"]
        if gaps:
            g = gaps.get(name) or {}
            c += [f"{g['gap_km']:.1f}" if g else "-", f"{g['km_in_runs_over_300m']:.1f} ({g['share_in_runs_over_300m']:.0%})" if g else "-"]
        L.append("| " + " | ".join(c) + " |")
    return L


def calibration(a):
    root = Path(a.root)
    rel = jl(root / "prc" / "mix12k_cal" / "reliability.json")
    us_raw = jl(root / "uncertainty_mix12k" / "uncertainty_stats.json") or jl(root / "uncertainty_mix12k" / "threshold_sweep.json")
    if us_raw and "stats" in us_raw:
        us_raw = us_raw["stats"]
    sw = jl(root / "uncertainty_mix12k_cal" / "threshold_sweep.json")
    L = ["# Experiment 1: calibrating the U-Net road score (mix12k, out of fold)", "",
         "*For each fold, an isotonic (monotone) mapping from the 256 score levels to the share of BD TOPO road pixels, fitted on the three "
         "other folds (`scripts/calibrate.py`). The calibrated value is the chance that a pixel is in the road label band (BD TOPO centre "
         "lines drawn 3 px wide, the training label). The order of the scores is unchanged, so the traced network and the routes are kept "
         "exactly (`outputs/hackathon100k/prc/mix12k_cal` reuses the base network); only the tools that read probability values change. "
         "Evaluation on the `examples` data only.*", "", "## 1. Reliability before and after", ""]
    L += rel_table(rel["raw"], "raw score") + [""] + rel_table(rel["calibrated"], "calibrated") + [""]
    L += [f"The raw tracing threshold 0.5 is a calibrated {', '.join(f'{v:.2f}' for v in rel['raw_0.5_on_calibrated_scale'].values())} "
          "(folds 0-3): a pixel traced as road has about a 25% chance of lying in the 3 px label band. The calibrated values stop near 0.75, "
          "because the map draws roads wider than the label band: even a sure road pixel is in the band about 3 times out of 4.", "",
          "## 2. Bridging with honest thresholds (bridges added to the base network, max 150 m)", ""]
    L += bridge_rows(root, [("raw scores: min 0.15, mean 0.3 (earlier)", "bridge_check_150"),
                            ("calibrated: min 0.15, mean 0.3", "bridge_check_cal_15_30"),
                            ("calibrated: min 0.3, mean 0.5", "bridge_check_cal_30_50"),
                            ("calibrated: min 0.4, mean 0.6", "bridge_check_cal_40_60")])
    L += ["", "## 3. Routes with the calibrated bridges", ""]
    L += route_rows(root, "ablation_cal", "exercise_ablation_cal",
                    [("base (no bridges)", "base"), ("calibrated min 0.15 / mean 0.3", "bridgecal_15_30"),
                     ("calibrated min 0.3 / mean 0.5", "bridgecal_30_50"), ("calibrated min 0.4 / mean 0.6", "bridgecal_40_60")])
    L += ["", "## 4. Uncertainty map and the operator's threshold on the calibrated scale", ""]
    us = sw["stats"] if sw else None
    if us and us_raw:
        L += ["| measure | raw score | calibrated |", "| --- | --- | --- |",
              f"| Spearman, uncertainty vs share of BD TOPO road missed | {us_raw['spearman_uncertainty_vs_missed']:.2f} | {us['spearman_uncertainty_vs_missed']:.2f} |",
              f"| BD TOPO road missed in the 20% most uncertain cells (lift) | {us_raw['missed_share_top20_uncertain']:.1%} (x{us_raw['lift_top20']:.1f}) | "
              f"{us['missed_share_top20_uncertain']:.1%} (x{us['lift_top20']:.1f}) |",
              f"| exercise gap points in those cells | {us_raw.get('gap_points_in_top20_uncertain_cells', float('nan')):.0%} | "
              f"{us.get('gap_points_in_top20_uncertain_cells', float('nan')):.0%} |", ""]
    if sw:
        import pandas as pd

        df = pd.DataFrame(sw["rows"])
        df["t"] = df["tau"].map(lambda t: "no penalty" if pd.isna(t) else f"{t:g}")
        L += ["Operator's threshold on the calibrated probability (sections below it cost 1 + 3 x the share below):", "",
              "| threshold (calibrated) | network km below it | length gap vs BD TOPO | overlap with BD TOPO | length gap vs OSM | overlap with OSM |",
              "| --- | --- | --- | --- | --- | --- |"]
        for t, d in df.groupby("t", sort=False):
            L.append(f"| {t} | {d['network_km_below_tau'].iloc[0]:,.0f} | {d['BD TOPO length gap'].abs().mean():.1%} | {d['BD TOPO overlap'].mean():.1%} | "
                     f"{d['OSM length gap'].abs().mean():.1%} | {d['OSM overlap'].mean():.1%} |")
    Path(a.doc).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


def loss(a):
    root = Path(a.root)
    prc = root / "prc"
    rs = {r["tag"]: r for r in pixel_runs(prc, a.runs)}
    desc = {}
    for t in a.runs:
        c = jl(prc / f"prc_{t}_config.json") or {}
        desc[t] = f"BCE x{c.get('pos_weight', 3):g}" + (f" + {c.get('dice_weight', 1):g} x Dice" if c.get("dice_weight", 1) else "")
    L = ["# Experiment 2: does the Dice loss matter? (road loss ablation against mix12k)", "",
         "*Same data and settings as mix12k (Hackathon training folds + Scenario 1 at 50% of the tiles, 12,000 steps, batch 16, lr 3e-4); only "
         "the road loss changes. Evaluation on the `examples` data only, out of fold, base inference and base tracing (p >= 0.5).*", "",
         "## 1. Pixels and calibration", "",
         "| run | road loss | road AUC | road IoU | importance | calibration error | share of road among pixels scored 0.4-0.6 | pixels scored >= 0.5 |",
         "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for t in a.runs:
        r = rs.get(t)
        rel = jl(prc / t / "reliability.json")
        mid = pos = "-"
        if rel:
            rr = rel["raw"]["reliability"]
            b = [x for x in rr if x["bin"] in ("0.4-0.5", "0.5-0.6")]
            if b:
                w = sum(x["share_pixels"] for x in b)
                mid = f"{sum(x['road_rate'] * x['share_pixels'] for x in b) / w:.0%}"
            pos = f"{sum(x['share_pixels'] for x in rr if float(x['bin'].split('-')[0]) >= 0.5):.1%}"
        cells = [t, desc[t]] + ([f"{r['road_auc']:.4f}", f"{r['road_iou']:.4f}", f"{r['importance_accuracy']:.1%}"] if r else ["-"] * 3)
        cells += [f"{rel['raw']['ece']:.3f}", mid, pos] if rel else ["-"] * 3
        L.append("| " + " | ".join(cells) + " |")
    L += ["", "## 2. Training time", "", "| run | fold minutes | GPU-hours |", "| --- | --- | --- |"]
    for t in a.runs:
        ds = [d for d in (train_log(prc / "logs" / f"ft_{t}_{f}.log") for f in range(4)) if d]
        if ds:
            L.append(f"| {t} | {', '.join('%.1f' % (d.get('total_seconds', 0) / 60) for d in ds)} | {sum(d.get('total_seconds', 0) for d in ds) / 3600:.2f} |")
    gaps = {t: jl(root / f"mismatch_loss_{t}" / "gap_runs.json") for t in a.runs}
    L += ["", "## 3. Missing roads, routes and the 10 exercises", ""]
    L += route_rows(root, "ablation_loss", "exercise_ablation_loss", [(f"{t} ({desc[t]})", t) for t in a.runs], gaps)
    Path(a.doc).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("calibration", "loss"))
    ap.add_argument("--root", required=True)
    ap.add_argument("--runs", nargs="*")
    ap.add_argument("--doc", required=True)
    a = ap.parse_args()
    {"calibration": calibration, "loss": loss}[a.cmd](a)

"""Report of the tracing variants (hysteresis, gap bridging) of one U-Net raster.

    python scripts/retrace_report.py --root outputs/hackathon100k --variants base hyst0.3 ... --doc docs/prc_retrace_results.md
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def jload(p: Path):
    return json.loads(p.read_text()) if p.exists() else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--variants", nargs="+", required=True)
    ap.add_argument("--doc", required=True)
    a = ap.parse_args()
    root = Path(a.root)
    rt = root / "retrace"
    st = {v: jload(rt / v / "retrace_stats.json") or {} for v in a.variants}
    L = ["# Tracing the road network from the U-Net probabilities: hysteresis and gap bridging", "",
         "*Same out-of-fold probabilities (PRC mix12k, base inference) for every variant; only the tracing changes. Evaluation on the "
         "`examples` data only. Hysteresis `low=t`: road pixels are the connected areas of p >= t that reach p >= 0.5 somewhere. Bridging "
         "`bridge=R`: each dead end is joined to the road it points at (within R m and 50 degrees) when the probability along the straight "
         "link stays >= 0.15 with a mean >= 0.3 and the two ends are not already joined nearby (network distance < 3 x the link).*", "",
         "## 1. The traced network", "",
         "| variant | tracing | sections | km | dead ends | separate pieces | largest connected share (nodes) | bridges (km) | tracing time |",
         "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for v in a.variants:
        s = st[v]
        de = f"{s.get('dead_ends_before', s.get('dead_ends', 0)):,} -> {s['dead_ends']:,}" if s.get("dead_ends_before") else f"{s.get('dead_ends', 0):,}"
        L.append(f"| {v} | {s.get('spec', '-')} | {s.get('sections', 0):,} | {s.get('km', 0):,} | {de} | {s.get('components', 0):,} | "
                 f"{s.get('largest_component_share_of_nodes', 0):.1%} | " + (f"{s['bridges']:,} ({s.get('link_km', 0)} km)" if s.get("bridges") else "-")
                 + f" | {s.get('seconds', 0):.0f} s |")
    abl = jload(root / "ablation_retrace" / "ablation.json") or {"rows": []}
    exa = jload(root / "exercise_ablation_retrace" / "exercise_ablation.json") or []
    L += ["", "## 2. Routes, exercises and gaps", "",
          "| variant | network within 25 m of BD TOPO | route score town / random | town / random time gap | pairs reached | 10 exercises: length gap BD TOPO / OSM | "
          "overlap BD TOPO / OSM | gap km (BD TOPO roads not followed) | broken-corridor km | road missed km | mismatch zones |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    per_ex = {}
    for v in a.variants:
        ar = next((r for r in abl["rows"] if r["variant"].startswith(f"PRC {v} ")), None)
        ex = [r for r in exa if r.get("network") == f"PRC {v}" and "error" not in r]
        per_ex[v] = {r["exercise"]: r for r in ex}
        mm = jload(root / f"mismatch_retrace_{v}" / "mismatch.json")
        cells = [v]
        cells += [f"{ar['within_25m']:.1%}", f"{ar['town']['score']:.3f} / {ar['random']['score']:.3f}",
                  f"{ar['town']['time_gap']:+.1%} / {ar['random']['time_gap']:+.1%}", f"{ar['reached']:.0%}"] if ar else ["-"] * 4
        if ex:
            m = lambda k: sum(abs(r[k]) if "gap" in k else r[k] for r in ex) / len(ex)
            cells += [f"{m('BD TOPO length gap'):.1%} / {m('OSM length gap'):.1%}" + ("" if len(ex) == 10 else f" ({len(ex)}/10)"),
                      f"{m('BD TOPO overlap'):.1%} / {m('OSM overlap'):.1%}"]
        else:
            cells += ["-", "-"]
        if mm:
            zs = [z for e in mm for z in e["zones"]]
            cause = lambda c: sum(z.get("BD TOPO km", 0) for z in zs if z.get("cause vs BD TOPO") == c)
            cells += [f"{sum(z.get('gap km on BD TOPO road', 0) for z in zs):.1f}", f"{cause('road seen, broken in the PRC network'):.1f}",
                      f"{cause('road missed by PRC'):.1f}", f"{len(zs)}"]
        else:
            cells += ["-"] * 4
        L.append("| " + " | ".join(cells) + " |")
    L += ["", "### Per exercise: length gap / overlap against BD TOPO", "",
          "| variant | " + " | ".join(f"ex {i}" for i in range(1, 11)) + " |", "| --- |" + " --- |" * 10]
    for v in a.variants:
        L.append(f"| {v} | " + " | ".join(f"{per_ex[v][i]['BD TOPO length gap']:+.1%} / {per_ex[v][i]['BD TOPO overlap']:.0%}" if i in per_ex[v] else "no route"
                                         for i in range(1, 11)) + " |")
    L += ["", "Maps of each variant's mismatches, with the PRC probability and the gaps: `outputs/hackathon100k/mismatch_retrace_<variant>/`. "
          "The traced networks (`network.pkl`, `tracing.json`) are in `outputs/hackathon100k/retrace/<variant>/` and are used as they are by "
          "`coa-net pdf-exercises --network prc-alone --unet outputs/hackathon100k/retrace/<variant>`."]
    Path(a.doc).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()

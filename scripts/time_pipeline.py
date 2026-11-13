"""Time the CPU part of the PRC pipeline for each probability raster: load, trace the road network, build the routing
network (built-up share, legal speeds), solve the 10 PDF exercises. Writes OUT/pipeline_timing.json.

    python scripts/time_pipeline.py --pdf examples/exercise_1-to-10-1.pdf --builtup B.geojson --raster NAME=DIR ... --out OUT
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

import geopandas as gpd

from coa_net import pdfexercises as px
from coa_net import scanunet


def cpu_info() -> dict:
    model = ""
    try:
        model = next(l.split(":", 1)[1].strip() for l in subprocess.run(["lscpu"], capture_output=True, text=True).stdout.splitlines()
                     if l.startswith("Model name"))
    except Exception:  # noqa: BLE001
        pass
    return {"node": socket.gethostname(), "cpu": model, "cores_allocated": int(os.environ.get("SLURM_CPUS_PER_TASK", os.cpu_count())),
            "threads": int(os.environ.get("OMP_NUM_THREADS", 0) or 0), "python": platform.python_version()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--builtup", required=True)
    ap.add_argument("--raster", action="append", required=True, metavar="NAME=DIR")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    builtup = gpd.read_file(a.builtup)
    _, exs = px.read_pdf(a.pdf)
    rows = []
    for spec in a.raster:
        name, d = spec.split("=", 1)
        T = {}
        t = time.perf_counter()
        P, tr = scanunet.load_raster(Path(d) / "unet_oof.tif")
        T["load raster (s)"] = time.perf_counter() - t
        t = time.perf_counter()
        tj = Path(d) / "tracing.json"
        G, _ = scanunet.trace_network(P, tr, json.loads(tj.read_text()) if tj.exists() else None)
        T["trace the road network (s)"] = time.perf_counter() - t
        t = time.perf_counter()
        N = px.Network(G, name, builtup=builtup)
        T["routing network: lines, built-up share, speeds (s)"] = time.perf_counter() - t
        t = time.perf_counter()
        per = {}
        for ex in exs:
            t1 = time.perf_counter()
            px.solve(ex, N)
            per[ex.number] = round(time.perf_counter() - t1, 2)
        T["solve the 10 exercises (s)"] = time.perf_counter() - t
        rows.append({"raster": name, **{k: round(v, 2) for k, v in T.items()}, "per_exercise_s": per,
                     "network_sections": G.number_of_edges() // 2, "network_km": round(sum(d_["length_m"] for _, _, d_ in G.edges(data=True)) / 2000, 0)})
        print(rows[-1], flush=True)
    (out / "pipeline_timing.json").write_text(json.dumps({"hardware": cpu_info(), "rows": rows}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

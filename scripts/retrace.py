"""Trace the road network of a U-Net raster several ways (no retraining) and save each as a folder usable everywhere
(`unet_oof.tif` link, `tracing.json`, `network.pkl`, `retrace_stats.json`).

    python scripts/retrace.py --raster outputs/x/prc/mix12k --out outputs/x/retrace \
        --variant base= --variant hyst0.3=low=0.3 --variant bridge150=bridge=150 ...
"""
from __future__ import annotations

import argparse
import json
import pickle
import time
from pathlib import Path

import networkx as nx

from coa_net import scanunet


def stats(G: nx.DiGraph) -> dict:
    U = nx.Graph()
    U.add_edges_from((a, b) for a, b in G.edges if a != b)
    comps = sorted((len(c) for c in nx.connected_components(U)), reverse=True)
    deg = dict(U.degree())
    km = sum(d["length_m"] for _, _, d in G.edges(data=True)) / 2000
    return {"sections": G.number_of_edges() // 2, "km": round(km), "nodes": U.number_of_nodes(),
            "dead_ends": sum(1 for v in deg.values() if v == 1), "components": len(comps),
            "largest_component_share_of_nodes": round(comps[0] / max(sum(comps), 1), 3) if comps else 0.0,
            "bridges": sum(1 for _, _, d in G.edges(data=True) if d.get("unet_bridge")) // 2}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raster", required=True, help="folder with unet_oof.tif")
    ap.add_argument("--out", required=True)
    ap.add_argument("--variant", action="append", required=True, metavar="NAME=SPEC", help="e.g. hyst0.3=low=0.3 ; base= for p >= 0.5")
    a = ap.parse_args()
    src = Path(a.raster).resolve() / "unet_oof.tif"
    P, tr = scanunet.load_raster(src)
    rows = []
    for v in a.variant:
        name, spec = v.split("=", 1)
        d = Path(a.out) / name
        d.mkdir(parents=True, exist_ok=True)
        link = d / "unet_oof.tif"
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(src)
        t = time.perf_counter()
        G, info = scanunet.trace_network(P, tr, spec or None)
        sec = time.perf_counter() - t
        (d / "tracing.json").write_text(json.dumps(scanunet.parse_tracing(spec or None)), encoding="utf-8")
        with open(d / "network.pkl", "wb") as f:
            pickle.dump(G, f)
        st = {"variant": name, "spec": spec or "p >= 0.5", "seconds": round(sec, 1), **stats(G), **info.get("bridging", {})}
        (d / "retrace_stats.json").write_text(json.dumps(st, indent=1), encoding="utf-8")
        rows.append(st)
        print(st, flush=True)
    (Path(a.out) / "retrace_stats.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

"""Gap points of a mismatch analysis (mismatch_map.py: BD TOPO road with no U-Net edge within 25 m, every 10 m) grouped
into runs along the road: how much of the gap length is long stretches the U-Net does not trace (missing roads) and how
much short breaks next to a dead end.

    python scripts/gap_runs.py --mismatch outputs/x/mismatch_... --network outputs/x/prc/RUN [--out runs.json]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree

from coa_net import scanunet


def gap_runs(mismatch: Path, network_dir: Path) -> dict:
    gp = Path(mismatch) / "prc_gaps.geojson"
    if not gp.exists():
        return {}
    g = gpd.read_file(gp)
    g = g[g.reference == "BD TOPO"].to_crs(2154)
    if not len(g):
        return {"gap_km": 0.0}
    xy = np.column_stack([g.geometry.x, g.geometry.y])
    p = g.p_road.values
    G = nx.Graph()
    G.add_nodes_from(range(len(xy)))
    G.add_edges_from(cKDTree(xy).query_pairs(15))
    runs = [np.array(sorted(c)) for c in nx.connected_components(G)]
    L = np.array([len(r) * 10.0 for r in runs])
    pmin = np.array([p[r].min() for r in runs])
    N = scanunet.unet_network(network_dir)
    U = nx.Graph()
    U.add_edges_from((a, b) for a, b in N.edges if a != b)
    dead = [n for n, d in U.degree() if d == 1]
    tr = Transformer.from_crs(4326, 2154, always_xy=True)
    dx, dy = tr.transform([N.nodes[n]["x"] for n in dead], [N.nodes[n]["y"] for n in dead])
    dt = cKDTree(np.column_stack([dx, dy]))
    near = np.array([dt.query(xy[r])[0].min() <= 60 for r in runs])
    long_ = L > 300
    return {"gap_km": round(L.sum() / 1000, 1), "runs": len(runs), "km_in_runs_over_300m": round(L[long_].sum() / 1000, 1),
            "share_in_runs_over_300m": round(float(L[long_].sum() / max(L.sum(), 1)), 3),
            "long_runs_with_p_below_0.15": round(float(np.mean(pmin[long_] < 0.15)) if long_.any() else 0.0, 3),
            "bridgeable_km": round(float(L[(L <= 150) & (pmin >= 0.15) & near].sum() / 1000), 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mismatch", required=True)
    ap.add_argument("--network", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    r = gap_runs(Path(a.mismatch), Path(a.network))
    print(json.dumps(r, indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(r, indent=1), encoding="utf-8")

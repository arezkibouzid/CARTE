"""Figures of docs/image_input_method.md: the road network read from the 1:100 000 scan, whole sheet and details,
with BD TOPO drawn on top of the details (check only).

    python docs/figures/scan/make_scan_figures.py --data DATA --cache outputs/scan_roads/cache --out docs/figures/scan

``--cache`` is the cache folder of a ``coa-net scan-roads`` run (the network is read again if it is missing).
"""

from __future__ import annotations

import argparse
from pathlib import Path

DETAILS = {
    # name: (lon_min, lat_min, lon_max, lat_max), what it shows
    "detail-mauron": ((-2.36, 48.05, -2.32, 48.075), "minor roads, tracks, a departmental road, a crossroads in a hamlet"),
    "detail-n24": ((-2.103, 47.943, -2.087, 47.955), "expressway (two carriageways), a road crossing it without a junction"),
}


def main(argv=None) -> None:
    from coa_net.realdata import BDTOPO_EXCLUDED, read_lines
    from coa_net.scanroads import render_network
    from coa_net.scenario import ScenarioPaths, load_graph_cached

    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--cache", default="outputs/scan_roads/cache")
    ap.add_argument("--out", default=str(Path(__file__).parent))
    a = ap.parse_args(argv)
    paths = ScenarioPaths.from_dir(a.data)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    Path(a.cache).mkdir(parents=True, exist_ok=True)
    G, _, _ = load_graph_cached(paths.scan100, Path(a.cache), kind="scan")
    render_network(G, paths.scan100, out / "network-100k.png", dpi=90)
    for name, (b, what) in DETAILS.items():
        ref, _ = read_lines(paths.bdtopo, "cp1252", ["NATURE"], bbox=b)
        ref = ref[~ref["NATURE"].str.lower().isin(BDTOPO_EXCLUDED)]
        render_network(G, paths.scan100, out / f"{name}.png", bounds=b, ref_lines=ref, dpi=100,
                       title=f"Read from the scan: {what}")
        print(f"{name}.png")


if __name__ == "__main__":
    main()

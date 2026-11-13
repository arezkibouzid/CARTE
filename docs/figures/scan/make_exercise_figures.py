"""Figures of exercise 1 (the exercise PDF: three separate itineraries, South zone to North zone) solved on each
road network: the image only, the image with the learned corrector, BD TOPO and OSM.

    python docs/figures/scan/make_exercise_figures.py --tif "DATA/Scenario1 100K.tif" \
        --run "image only=out/ex_scan" --run "image + corrector=out/ex_corr" \
        --run "BD TOPO=out/ex_bd" --run "OSM=out/ex_osm" --out docs/figures/scan

Each ``--run`` is a ``coa-net exercise`` output folder (``itineraries.geojson``, ``zones.geojson``,
``towns_over_threshold.geojson``, ``metrics.json``). Writes ``exercise1-networks.png`` (one panel per network on the
1:100 000 scan), ``exercise1-overlay.png`` (every network's itineraries on one map) and ``exercise1-bars.png``
(length and time of each itinerary).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

IT_COLOURS = {"IT-1": "#d62728", "IT-2": "#1f77b4", "IT-3": "#2ca02c"}
NET_COLOURS = ["#e6550d", "#756bb1", "#d62728", "#31a354", "#3182bd", "#636363"]


def _load(folder: Path) -> dict:
    gj = lambda n: json.loads((folder / n).read_text()) if (folder / n).exists() else {"features": []}
    its = [f for f in gj("itineraries.geojson")["features"] if f["properties"].get("kind") == "itinerary"]
    return {"its": its, "zones": gj("zones.geojson")["features"], "towns": gj("towns_over_threshold.geojson")["features"]}


def _background(tif: str, bounds, max_px: int = 2400):
    import numpy as np
    import rasterio
    from rasterio.windows import from_bounds

    with rasterio.open(tif) as src:
        w = from_bounds(*bounds, transform=src.transform)
        f = max(1, int(max(w.width, w.height) / max_px))
        shape = (int(w.height / f), int(w.width / f))
        if src.count == 1 and src.colorinterp[0].name == "palette":
            idx = src.read(1, window=w, out_shape=shape)
            cmap = src.colormap(1)
            lut = np.zeros((256, 3), np.uint8)
            for k, v in cmap.items():
                lut[k] = v[:3]
            rgb = lut[idx]
        else:
            rgb = np.moveaxis(src.read([1, 2, 3], window=w, out_shape=(3, *shape)), 0, -1)
    return rgb


def _draw(ax, img, bounds, run, title, lw=2.6):
    from matplotlib.patches import Polygon

    lon0, lat0, lon1, lat1 = bounds
    ax.imshow(img, extent=(lon0, lon1, lat0, lat1), alpha=0.55, interpolation="lanczos")
    for z in run["zones"]:
        for ring in ([z["geometry"]["coordinates"][0]] if z["geometry"]["type"] == "Polygon" else [p[0] for p in z["geometry"]["coordinates"]]):
            ax.add_patch(Polygon(ring, closed=True, fill=False, ec="#08306b", lw=1.4, ls="--"))
        c = z["geometry"]["coordinates"][0] if z["geometry"]["type"] == "Polygon" else z["geometry"]["coordinates"][0][0]
        xs, ys = zip(*c)
        ax.text(sum(xs) / len(xs), sum(ys) / len(ys), z["properties"].get("name", ""), color="#08306b", fontsize=9,
                ha="center", weight="bold", bbox=dict(fc="white", ec="none", alpha=0.6, pad=1))
    for t in run["towns"]:
        g = t["geometry"]
        if g["type"] == "Point":
            x, y = g["coordinates"]
        else:
            import shapely.geometry as sg

            p = sg.shape(g).centroid
            x, y = p.x, p.y
        if not (lon0 <= x <= lon1 and lat0 <= y <= lat1):
            continue
        ax.plot(x, y, "s", ms=6, mfc="none", mec="black", mew=1.2)
        ax.text(x, y + 0.012, f"{t['properties'].get('town', '').title()}", fontsize=7, ha="center")
    for f in run["its"]:
        p = f["properties"]
        xs, ys = zip(*f["geometry"]["coordinates"])
        ax.plot(xs, ys, color="white", lw=lw + 2.2, solid_capstyle="round")
        ax.plot(xs, ys, color=IT_COLOURS.get(p["id"], "k"), lw=lw, solid_capstyle="round",
                label=f"{p['id']}: {p['length_km']:.1f} km, {p['time_min']:.0f} min")
    ax.set_xlim(lon0, lon1)
    ax.set_ylim(lat0, lat1)
    ax.set_aspect(1 / 0.67)
    ax.set_title(title, fontsize=11, weight="bold")
    ax.legend(loc="lower right", fontsize=8, framealpha=0.85)
    ax.tick_params(labelsize=7)


def main(argv=None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ap = argparse.ArgumentParser()
    ap.add_argument("--tif", required=True)
    ap.add_argument("--run", action="append", required=True, help="LABEL=FOLDER")
    ap.add_argument("--out", default=str(Path(__file__).parent))
    a = ap.parse_args(argv)
    runs = {}
    for r in a.run:
        lab, folder = r.split("=", 1)
        runs[lab] = _load(Path(folder))
    xs = [x for r in runs.values() for f in r["its"] for x, _ in f["geometry"]["coordinates"]]
    ys = [y for r in runs.values() for f in r["its"] for _, y in f["geometry"]["coordinates"]]
    for r in runs.values():
        for z in r["zones"]:
            c = z["geometry"]["coordinates"][0] if z["geometry"]["type"] == "Polygon" else z["geometry"]["coordinates"][0][0]
            xs += [p[0] for p in c]
            ys += [p[1] for p in c]
    m = 0.03
    bounds = (min(xs) - m, min(ys) - m, max(xs) + m, max(ys) + m)
    img = _background(a.tif, bounds)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    n = len(runs)
    cols = 2 if n > 1 else 1
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(6.0 * cols, 7.4 * rows), squeeze=False)
    for ax, (lab, r) in zip(axes.flat, runs.items()):
        tot = sum(f["properties"]["length_km"] for f in r["its"])
        _draw(ax, img, bounds, r, f"{lab}: {len(r['its'])} itineraries, {tot:.0f} km")
    for ax in list(axes.flat)[n:]:
        ax.axis("off")
    fig.suptitle("Exercise 1: three separate itineraries, South zone to North zone\n(at least 5 km apart, towns over 10,000 inhabitants avoided)",
                 fontsize=13, weight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    fig.savefig(out / "exercise1-networks.png", dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 10.5))
    lon0, lat0, lon1, lat1 = bounds
    ax.imshow(img, extent=(lon0, lon1, lat0, lat1), alpha=0.5, interpolation="lanczos")
    styles = ["-", "--", "-", "-.", ":", "--"]
    for i, (lab, r) in enumerate(runs.items()):
        for j, f in enumerate(r["its"]):
            xs_, ys_ = zip(*f["geometry"]["coordinates"])
            ax.plot(xs_, ys_, color=NET_COLOURS[i % len(NET_COLOURS)], lw=max(1.2, 3.4 - 0.45 * i), ls=styles[i % len(styles)], label=lab if j == 0 else None, alpha=0.9)
    _draw(ax, img, bounds, {"zones": next(iter(runs.values()))["zones"], "towns": next(iter(runs.values()))["towns"], "its": []},
          "Exercise 1: the itineraries of every network on one map")
    ax.legend(loc="lower right", fontsize=9, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(out / "exercise1-overlay.png", dpi=110)
    plt.close(fig)

    import numpy as np

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4.2))
    labs = list(runs)
    width = 0.8 / max(len(labs), 1)
    for i, lab in enumerate(labs):
        its = sorted(runs[lab]["its"], key=lambda f: f["properties"]["length_km"])
        x = np.arange(len(its)) + i * width
        a1.bar(x, [f["properties"]["length_km"] for f in its], width, color=NET_COLOURS[i % len(NET_COLOURS)], label=lab)
        a2.bar(x, [f["properties"]["time_min"] for f in its], width, color=NET_COLOURS[i % len(NET_COLOURS)], label=lab)
    for ax_, yl in ((a1, "length (km)"), (a2, "time (min)")):
        ax_.set_xticks(np.arange(3) + width * (len(labs) - 1) / 2, ["shortest", "middle", "longest"])
        ax_.set_ylabel(yl)
        ax_.grid(axis="y", alpha=0.3)
    a1.legend(fontsize=8)
    fig.suptitle("Exercise 1: itineraries per network, sorted by length", weight="bold")
    fig.tight_layout()
    fig.savefig(out / "exercise1-bars.png", dpi=110)
    plt.close(fig)
    print("exercise1-networks.png, exercise1-overlay.png, exercise1-bars.png")


if __name__ == "__main__":
    main()

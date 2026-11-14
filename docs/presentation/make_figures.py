"""Images of the vision slides, from the real rasters: one window of the scan through every processing step, and what each
U-Net version reads on it.

    .venv/bin/python docs/presentation/make_figures.py

Writes img/step_*.png (scan, probability, classes, mask, centre lines, graph), img/ver_*.png (zero-shot, 1 sheet,
2 sheets) and img/var_*.png (the four variants of the app), all on the same 640 x 640 px window (the U-Net reads 512 px
tiles). Out-of-fold rasters: each pixel predicted by a model that never saw it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw
from rasterio.windows import Window

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path[:0] = [str(REPO), str(REPO / "src")]
SCAN = REPO / "examples/Hackathon_100K.tif"
PRC = REPO / "outputs/hackathon100k/prc"
CENTER, N = (5.15, 49.05), 640
OUT = HERE / "img"
CLS = np.array([[200, 56, 47], [235, 104, 52], [237, 161, 0], [27, 175, 122], [42, 120, 214]], np.uint8)


def window(path):
    with rasterio.open(path) as r:
        row, col = r.index(*CENTER)
        w = Window(col - N // 2, row - N // 2, N, N)
        a = r.read(window=w)
        tr = r.window_transform(w)
        if r.count == 1 and r.colormap(1):
            cm = r.colormap(1)
            lut = np.array([cm.get(i, (0, 0, 0, 0))[:3] for i in range(256)], np.uint8)
            return lut[a[0]], tr
    return np.moveaxis(a, 0, -1), tr


def save(a, name, size=N):
    Image.fromarray(a.astype(np.uint8)).resize((size, size), Image.LANCZOS).save(OUT / name)


def faded(rgb, k=0.45):
    return (rgb.astype(float) * k + 255 * (1 - k)).astype(np.uint8)


def overlay(rgb, mask, color):
    out = faded(rgb, 0.4)
    out[mask] = color
    return out


def main():
    from scipy import ndimage
    from skimage.morphology import skeletonize

    from coa_net.scanunet import network_from_unet

    rgb, tr = window(SCAN)
    P, ptr = window(PRC / "mix12k_nodice/unet_oof.tif")
    p = P[..., 0].astype(float) / 255
    save(rgb, "step_1_scan.png")
    import matplotlib

    heat = (matplotlib.colormaps["magma"](1 - p * 0.92)[..., :3] * 255).astype(np.uint8)
    save(heat, "step_2_probability.png")
    road = ndimage.binary_dilation(p >= 0.5, iterations=1)
    cls = np.argmax(P[..., 1:6], -1)
    c = np.full(rgb.shape, 255, np.uint8)
    c[road] = CLS[cls[road]]
    save(c, "step_3_classes.png")
    m = np.full(rgb.shape, 255, np.uint8)
    m[p >= 0.5] = (20, 35, 42)
    save(m, "step_4_mask.png")
    sk = ndimage.binary_dilation(skeletonize(p >= 0.5), iterations=1)
    s = np.full(rgb.shape, 255, np.uint8)
    s[sk] = (200, 56, 47)
    save(s, "step_5_centrelines.png")
    G = network_from_unet(P, ptr, threshold=0.5)
    im = Image.fromarray(faded(rgb, 0.3))
    d = ImageDraw.Draw(im)
    inv = ~ptr
    px = lambda x, y: tuple(float(v) for v in inv * (x, y))
    for a, b, e in G.edges(data=True):
        pts = [px(x, y) for x, y in e["coords"]]
        d.line(pts, fill=(31, 122, 109), width=4)
    for n, e in G.nodes(data=True):
        x, y = px(e["x"], e["y"])
        d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=(255, 255, 255), outline=(200, 56, 47), width=3)
    im.resize((N, N), Image.LANCZOS).save(OUT / "step_6_graph.png")
    print("graph:", G.number_of_nodes(), "junctions,", G.number_of_edges(), "sections")

    for name, folder, color in [("ver_zeroshot.png", "zeroshot", (120, 120, 120)), ("ver_1sheet.png", "finetuned", (42, 120, 214)),
                                ("ver_2sheets.png", "mix12k_nodice", (31, 122, 109))]:
        Q, _ = window(PRC / folder / "unet_oof.tif")
        save(overlay(rgb, ndimage.binary_dilation(Q[..., 0] >= 128, iterations=1), color), name, 480)
    for name, folder in [("var_coverage.png", "mix12k_nodice"), ("var_compromise.png", "mix12k_nodice_tta"),
                         ("var_precision.png", "mix12k_tta"), ("var_reference.png", "mix12k")]:
        Q, _ = window(PRC / folder / "unet_oof.tif")
        save(overlay(rgb, ndimage.binary_dilation(Q[..., 0] >= 128, iterations=1), (200, 56, 47)), name, 420)


if __name__ == "__main__":
    main()

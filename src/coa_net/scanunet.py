"""A small U-Net on the scan, trained with BD TOPO drawn onto it: a road probability for every pixel.

The reader of :mod:`coa_net.scanroads` decides by fixed rules from colour classes, and the corrector of
:mod:`coa_net.scanlearn` corrects some of those decisions from features of the image network. A segmentation
network looks at the picture itself: it can see a road continue under an expressway band or a label, tell a road
from another line of the same colour, and see a bridge drawn across another road. This module trains one, with the
same spatial folds as the corrector, so every pixel's probability comes from a network that never saw its block.

* **Input**: the scan's RGB colours (palette looked up), tiles of ``TILE`` x ``TILE`` pixels.
* **Labels** (BD TOPO, training and scoring only), drawn ``LABEL_PX`` pixels wide:
  - output 1, *road*: 1 on BD TOPO roads a convoy can use (not tracks, paths, cycle tracks or steps), else 0;
  - output 2, *importance* 1 to 5 of those roads (BD TOPO ``IMPORTANCE``), scored on road pixels only;
  - output 3, *structure*: 1 on road sections above ground (``POS_SOL`` > 0 or ``FRANCHISST`` = bridge).
* **Network**: a 4-level U-Net (16 to 128 channels, about 0.5 M weights) with 7 output channels (1 + 5 + 1).
  Loss: binary cross-entropy for road and structure, cross-entropy for importance on road pixels; pixels of the
  held-out fold are masked out of the loss.
* **Outputs**: ``unet_oof.tif``, a 7-band uint8 GeoTIFF on the scan grid (p road, p importance 1..5 given road,
  p structure, x 255), every block predicted by the network of the fold that held it out; ``unet_final.pt``, trained
  on every block, for another sheet.

Its probabilities are used two ways: as features of the corrector (:func:`road_unet_features`,
:func:`link_unet_features`, :func:`point_unet_features`), and alone, as a road network of their own
(:func:`network_from_unet`), to measure what the network would be without the reader and the corrector.
"""

from __future__ import annotations

import math
import time
from pathlib import Path

import networkx as nx
import numpy as np

TILE = 192
LABEL_PX = 3
N_OUT = 7  # road, importance 1..5, structure
BANDS = ("p_road", "p_imp1", "p_imp2", "p_imp3", "p_imp4", "p_imp5", "p_structure")
NOT_ROAD = {"sentier", "escalier", "piste cyclable", "chemin", "route empierrée"}


# ---------------------------------------------------------------------------
# data


def scan_rgb(tif: str):
    """(H, W, 3) uint8 colours of the scan, its transform and bounds."""
    import rasterio

    with rasterio.open(tif) as src:
        b = src.bounds
        if src.count == 1:
            cm = src.colormap(1)
            lut = np.zeros((256, 3), np.uint8)
            for k, v in cm.items():
                lut[k] = v[:3]
            rgb = lut[src.read(1)]
        else:
            rgb = np.moveaxis(src.read([1, 2, 3]), 0, -1)
        return rgb, src.transform, (b.left, b.bottom, b.right, b.top), src.crs


def label_rasters(bdtopo: str, shape: tuple, transform):
    """(road, importance, structure) uint8 rasters on the scan grid; importance 0 = no label."""
    from rasterio.features import rasterize
    from scipy import ndimage

    from .realdata import read_lines

    bd, _ = read_lines(bdtopo, "cp1252", ["NATURE", "IMPORTANCE", "POS_SOL", "FRANCHISST"])
    bd = bd.to_crs("EPSG:4326")
    road = ~bd["NATURE"].fillna("").str.lower().isin(NOT_ROAD)
    imp = bd["IMPORTANCE"].astype(str).str.strip()
    imp = np.where(imp.isin(list("12345")), imp, "0").astype(int)
    struct = (bd["POS_SOL"].fillna(0).astype(int) > 0) | (bd["FRANCHISST"] == "Pont")
    k = np.ones((LABEL_PX, LABEL_PX), bool)

    def draw(geoms, values=None):
        shapes = list(zip(geoms, values)) if values is not None else [(g, 1) for g in geoms]
        return rasterize(shapes, out_shape=shape, transform=transform, fill=0, dtype="uint8", all_touched=True) if shapes else np.zeros(shape, np.uint8)

    r = ndimage.binary_dilation(draw(bd.geometry[road].values) > 0, k).astype(np.uint8)
    # importance: the most important road wins where lines overlap (drawn last)
    order = np.argsort(-imp[road.values])
    g = bd.geometry[road].values[order]
    v = imp[road.values][order]
    keep = v > 0
    im = draw(g[keep], v[keep])
    im = ndimage.grey_dilation(np.where(im == 0, 0, 6 - im).astype(np.uint8), footprint=k)
    im = np.where(im > 0, 6 - im, 0).astype(np.uint8) * r
    s = ndimage.binary_dilation(draw(bd.geometry[road & struct].values) > 0, k).astype(np.uint8)
    return r, im, s


def block_raster(shape: tuple, transform, bounds: tuple, grid: int) -> np.ndarray:
    """Block id of every pixel (the blocks of :func:`coa_net.scanlearn.blocks_of`)."""
    H, W = shape
    x0, y0, x1, y1 = bounds
    lon = transform.c + (np.arange(W) + 0.5) * transform.a
    lat = transform.f + (np.arange(H) + 0.5) * transform.e
    i = np.clip(((lon - x0) / (x1 - x0) * grid).astype(int), 0, grid - 1)
    j = np.clip(((lat - y0) / (y1 - y0) * grid).astype(int), 0, grid - 1)
    return (j[:, None] * grid + i[None, :]).astype(np.int16)


# ---------------------------------------------------------------------------
# network


def _net(base: int = 12):
    import torch
    import torch.nn as nn

    def conv(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True),
                             nn.Conv2d(o, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(inplace=True))

    class UNet(nn.Module):
        def __init__(self):
            super().__init__()
            c = [base, base * 2, base * 4, base * 8]
            self.e1, self.e2, self.e3, self.e4 = conv(3, c[0]), conv(c[0], c[1]), conv(c[1], c[2]), conv(c[2], c[3])
            self.pool = nn.MaxPool2d(2)
            self.u3, self.d3 = nn.ConvTranspose2d(c[3], c[2], 2, 2), conv(c[3], c[2])
            self.u2, self.d2 = nn.ConvTranspose2d(c[2], c[1], 2, 2), conv(c[2], c[1])
            self.u1, self.d1 = nn.ConvTranspose2d(c[1], c[0], 2, 2), conv(c[1], c[0])
            self.out = nn.Conv2d(c[0], N_OUT, 1)

        def forward(self, x):
            e1 = self.e1(x)
            e2 = self.e2(self.pool(e1))
            e3 = self.e3(self.pool(e2))
            e4 = self.e4(self.pool(e3))
            d3 = self.d3(torch.cat([self.u3(e4), e3], 1))
            d2 = self.d2(torch.cat([self.u2(d3), e2], 1))
            d1 = self.d1(torch.cat([self.u1(d2), e1], 1))
            return self.out(d1)

    return UNet()


def _loss(out, road, imp, struct, mask):
    import torch
    import torch.nn.functional as F

    m = mask.float()
    lr = F.binary_cross_entropy_with_logits(out[:, 0], road.float(), reduction="none", pos_weight=torch.tensor(3.0))
    ls = F.binary_cross_entropy_with_logits(out[:, 6], struct.float(), reduction="none", pos_weight=torch.tensor(30.0))
    li = F.cross_entropy(out[:, 1:6], (imp.long() - 1).clamp(min=0), reduction="none")
    mi = m * (imp > 0).float()
    return (lr * m).sum() / m.sum().clamp(min=1) + (ls * m).sum() / m.sum().clamp(min=1) + 0.5 * (li * mi).sum() / mi.sum().clamp(min=1)


def _aug(x, k):
    import torch

    x = torch.rot90(x, k % 4, dims=(-2, -1))
    return torch.flip(x, dims=(-1,)) if k >= 4 else x


def train(rgb, road, imp, struct, train_mask, steps: int = 1500, batch: int = 8, seed: int = 0, progress=print):
    """Train on random tiles centred in ``train_mask``; pixels outside it do not count in the loss."""
    import torch

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = _net()
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-3, total_steps=steps)
    H, W = road.shape
    # tile corners whose centre lies in a training block (coarse grid of candidate corners)
    rr, cc = np.nonzero(train_mask[TILE // 2:H - TILE // 2:32, TILE // 2:W - TILE // 2:32])
    corners = np.column_stack([rr * 32, cc * 32])
    t0 = time.perf_counter()
    net.train()
    for step in range(steps):
        pick = corners[rng.integers(0, len(corners), batch)]
        xs, rs, is_, ss, ms = [], [], [], [], []
        for r0, c0 in pick:
            sl = (slice(r0, r0 + TILE), slice(c0, c0 + TILE))
            xs.append(rgb[sl]); rs.append(road[sl]); is_.append(imp[sl]); ss.append(struct[sl]); ms.append(train_mask[sl])
        x = torch.from_numpy(np.stack(xs)).permute(0, 3, 1, 2).float() / 255.0
        y = [torch.from_numpy(np.stack(a)) for a in (rs, is_, ss, ms)]
        k = int(rng.integers(8))
        x = _aug(x, k)
        y = [_aug(a, k) for a in y]
        opt.zero_grad()
        loss = _loss(net(x), *y)
        loss.backward()
        opt.step()
        sched.step()
        if (step + 1) % 250 == 0:
            progress(f"    step {step + 1}/{steps}: loss {loss.item():.3f}, {time.perf_counter() - t0:.0f} s")
    net.eval()
    return net


def predict(net, rgb, where=None, tile: int = 512, margin: int = 32) -> np.ndarray:
    """(H, W, 7) uint8 probabilities x 255 (importance as probabilities given a road), on the pixels of ``where``."""
    import torch

    H, W = rgb.shape[:2]
    out = np.zeros((H, W, N_OUT), np.uint8)
    step = tile - 2 * margin
    with torch.no_grad():
        for r0 in range(0, H, step):
            for c0 in range(0, W, step):
                r1, c1 = min(r0 + step, H), min(c0 + step, W)
                if where is not None and not where[r0:r1, c0:c1].any():
                    continue
                a0, b0 = max(r0 - margin, 0), max(c0 - margin, 0)
                a1, b1 = min(r1 + margin, H), min(c1 + margin, W)
                x = torch.from_numpy(rgb[a0:a1, b0:b1]).permute(2, 0, 1).float()[None] / 255.0
                ph, pw = (-x.shape[2]) % 8, (-x.shape[3]) % 8
                x = torch.nn.functional.pad(x, (0, pw, 0, ph), mode="replicate")
                o = net(x)[0, :, :a1 - a0, :b1 - b0]
                p = torch.cat([torch.sigmoid(o[:1]), torch.softmax(o[1:6], 0), torch.sigmoid(o[6:])], 0)
                p = (p.permute(1, 2, 0).numpy() * 255 + 0.5).astype(np.uint8)
                tgt = p[r0 - a0:r1 - a0, c0 - b0:c1 - b0]
                if where is None:
                    out[r0:r1, c0:c1] = tgt
                else:
                    w = where[r0:r1, c0:c1]
                    out[r0:r1, c0:c1][w] = tgt[w]
    return out


def save_raster(path, P: np.ndarray, transform, crs) -> None:
    import rasterio

    with rasterio.open(path, "w", driver="GTiff", height=P.shape[0], width=P.shape[1], count=P.shape[2], dtype="uint8",
                       crs=crs, transform=transform, compress="deflate", tiled=True) as dst:
        for b in range(P.shape[2]):
            dst.write(P[:, :, b], b + 1)
            dst.set_band_description(b + 1, BANDS[b])


def load_raster(path):
    """(H, W, 7) uint8 probabilities and the transform."""
    import rasterio

    with rasterio.open(path) as src:
        return np.moveaxis(src.read(), 0, -1), src.transform


def scores(P: np.ndarray, road, imp, struct) -> dict:
    """Pixel scores of the out-of-fold probabilities: AUC and IoU of road, importance accuracy, structure AUC."""
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(0)
    H, W = road.shape
    idx = rng.integers(0, H * W, 2_000_000)
    pr, yr = P[..., 0].ravel()[idx] / 255.0, road.ravel()[idx]
    pred = P[..., 0] >= 128
    inter = np.logical_and(pred, road > 0).sum()
    union = np.logical_or(pred, road > 0).sum()
    m = imp > 0
    acc = float((np.argmax(P[..., 1:6][m], axis=1) + 1 == imp[m]).mean()) if m.any() else float("nan")
    w4 = m & (imp >= 4)
    acc45 = float(((np.argmax(P[..., 1:6][w4], axis=1) + 1) == imp[w4]).mean()) if w4.any() else float("nan")
    on = road > 0
    ps, ys = P[..., 6][on].ravel() / 255.0, struct[on].ravel()
    sub = rng.integers(0, len(ps), min(len(ps), 2_000_000))
    return {"road_auc": float(roc_auc_score(yr, pr)), "road_iou": float(inter / max(union, 1)),
            "importance_accuracy": acc, "importance_4_vs_5_accuracy": acc45,
            "structure_auc": float(roc_auc_score(ys[sub], ps[sub])) if ys[sub].any() else float("nan")}


def learn(tif: str, bdtopo: str, out: Path, grid: int = 4, folds_of_blocks=None, steps: int = 1500, final: bool = True,
          progress=print) -> dict:
    """Out-of-fold probabilities of the whole sheet (one network per fold), their pixel scores, and a final network."""
    import torch

    from .scanlearn import fold_of_block

    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(max(1, torch.get_num_threads()))
    t = time.perf_counter()
    rgb, transform, bounds, crs = scan_rgb(tif)
    road, imp, struct = label_rasters(bdtopo, rgb.shape[:2], transform)
    folds_of_blocks = np.asarray(folds_of_blocks if folds_of_blocks is not None else fold_of_block(grid * grid, 4, 0))
    fold = folds_of_blocks[block_raster(rgb.shape[:2], transform, bounds, grid)]
    progress(f"  U-Net labels: road pixels {road.mean():.1%}, structure pixels {struct.mean():.2%}, {time.perf_counter() - t:.0f} s")
    P = np.zeros(rgb.shape[:2] + (N_OUT,), np.uint8)
    for f in range(int(folds_of_blocks.max()) + 1):
        t = time.perf_counter()
        progress(f"  U-Net fold {f}: training on the other folds")
        net = train(rgb, road, imp, struct, fold != f, steps=steps, seed=f, progress=progress)
        test = fold == f
        P[test] = predict(net, rgb, where=test)[test]
        progress(f"  U-Net fold {f}: done, {time.perf_counter() - t:.0f} s")
    save_raster(out / "unet_oof.tif", P, transform, crs)
    sc = scores(P, road, imp, struct)
    progress(f"  U-Net on unseen blocks: road AUC {sc['road_auc']:.3f}, IoU {sc['road_iou']:.3f}; importance accuracy "
             f"{sc['importance_accuracy']:.1%} (4 vs 5: {sc['importance_4_vs_5_accuracy']:.1%}); structure AUC {sc['structure_auc']:.3f}")
    if final:
        net = train(rgb, road, imp, struct, np.ones_like(road, bool), steps=steps, seed=99, progress=progress)
        torch.save(net.state_dict(), str(out / "unet_final.pt"))
    import json

    (out / "unet_scores.json").write_text(json.dumps(sc, indent=1), encoding="utf-8")
    return sc


def predict_sheet(tif: str, model_path) -> tuple[np.ndarray, object]:
    """Probabilities of a sheet the network was not trained on."""
    import torch

    rgb, transform, _, _ = scan_rgb(tif)
    net = _net()
    net.load_state_dict(torch.load(str(model_path)))
    net.eval()
    return predict(net, rgb), transform


# ---------------------------------------------------------------------------
# features for the corrector


def _along(P: np.ndarray, rc: np.ndarray) -> np.ndarray:
    """(n, 7) probabilities (0..1) at pixel points; the max over a 3 x 3 neighbourhood (the label is 3 px wide)."""
    H, W = P.shape[:2]
    r = np.clip(np.rint(rc[:, 0]).astype(int), 1, H - 2)
    c = np.clip(np.rint(rc[:, 1]).astype(int), 1, W - 2)
    best = np.zeros((len(r), P.shape[2]), np.float32)
    for dr in (-1, 0, 1):
        for dc in (-1, 0, 1):
            v = P[r + dr, c + dc].astype(np.float32)
            take = v[:, 0] > best[:, 0]
            best[take] = v[take]
    return best / 255.0


def _line_stats(P, rc):
    v = _along(P, rc)
    pr = v[:, 0]
    imp = v[:, 1:6]
    e_imp = (imp * np.arange(1, 6)).sum(1) / np.maximum(imp.sum(1), 1e-6)
    return [float(pr.mean()), float(pr.min()), float((pr < 0.5).mean()), float(e_imp.mean()), float(imp[:, :4].sum(1).mean()),
            float(v[:, 6].max())]


LINE_FEATURES = ["unet_road_mean", "unet_road_min", "unet_road_gap_share", "unet_imp_mean", "unet_imp_le4", "unet_structure_max"]


def road_unet_features(roads, P: np.ndarray, transform):
    """U-Net features of every road of the road table."""
    import pandas as pd

    from .scanlearn import _densify_px, _to_px

    rows = []
    for g in roads.geometry.values:
        rc = _densify_px(_to_px(transform, np.asarray(g.coords)), 2.0)
        rows.append(_line_stats(P, rc))
    return pd.DataFrame(rows, columns=LINE_FEATURES)


def link_unet_features(G, links, P: np.ndarray, transform):
    """U-Net features along the straight line of each candidate link."""
    import pandas as pd

    from .scanlearn import _densify_px, _to_px

    rows = []
    for c in links:
        ab = np.array([[G.nodes[c["a"]]["x"], G.nodes[c["a"]]["y"]], [G.nodes[c["b"]]["x"], G.nodes[c["b"]]["y"]]])
        rc = _densify_px(_to_px(transform, ab), 2.0)
        rows.append(_line_stats(P, rc))
    return pd.DataFrame(rows, columns=[f.replace("unet_", "unet_link_") for f in LINE_FEATURES])


def point_unet_features(G, cands, P: np.ndarray, transform, radius_px: int = 6):
    """U-Net structure and road probabilities around each crossing junction."""
    import pandas as pd

    from .scanlearn import _to_px

    H, W = P.shape[:2]
    rows = []
    for c in cands:
        r, col = _to_px(transform, np.array([[G.nodes[c["node"]]["x"], G.nodes[c["node"]]["y"]]]))[0]
        r, col = int(round(r)), int(round(col))
        win = P[max(r - radius_px, 0):min(r + radius_px + 1, H), max(col - radius_px, 0):min(col + radius_px + 1, W)].astype(np.float32) / 255.0
        if win.size == 0:
            rows.append([0.0, 0.0, 0.0])
            continue
        rows.append([float(win[..., 6].max()), float(win[..., 6].mean()), float(win[..., 0].mean())])
    return pd.DataFrame(rows, columns=["unet_structure_max", "unet_structure_mean", "unet_road_mean"])


# ---------------------------------------------------------------------------
# the U-Net alone: a road network from its probabilities


def network_from_unet(P: np.ndarray, transform, threshold: float = 0.5, min_px: int = 30, step_px: int = 4,
                      low: float | None = None) -> nx.DiGraph:
    """Road graph from the road probability alone: threshold, thin to centre lines, trace, rank by the importance
    output. No reader, no corrector: what the segmentation network gives by itself.

    ``low`` (hysteresis): road pixels are the connected areas of p >= ``low`` that reach p >= ``threshold`` somewhere,
    so a weak stretch attached to a confident road is kept and an isolated weak blob is not."""
    from scipy import ndimage
    from skimage.morphology import skeletonize

    if low is None or low >= threshold:
        m = P[..., 0] >= int(threshold * 255)
    else:
        ml = P[..., 0] >= int(low * 255)
        lab, n = ndimage.label(ml, structure=np.ones((3, 3)))
        peak = ndimage.maximum(P[..., 0], lab, index=np.arange(1, n + 1)) if n else np.zeros(0)
        keep = np.concatenate([[False], np.asarray(peak) >= int(threshold * 255)])
        m = keep[lab]
    lab0, _ = ndimage.label(m, structure=np.ones((3, 3)))
    m = m & (np.bincount(lab0.ravel())[lab0] >= min_px)
    sk = skeletonize(m)
    nb = ndimage.convolve(sk.astype(np.uint8), np.ones((3, 3), np.uint8), mode="constant") - 1
    nb = np.where(sk, nb, 0)
    H, W = sk.shape
    node_px = sk & (nb != 2)
    ys, xs = np.nonzero(node_px)
    # junction pixels next to each other form one node
    lab, nn_ = ndimage.label(node_px, structure=np.ones((3, 3)))
    node_of = lab  # node id per node pixel (1..nn_)
    cent = ndimage.center_of_mass(node_px, lab, range(1, nn_ + 1))
    G = nx.DiGraph()

    def lonlat(r, c):
        return transform.c + (c + 0.5) * transform.a, transform.f + (r + 0.5) * transform.e

    for i, (r, c) in enumerate(cent, start=1):
        x, y = lonlat(r, c)
        G.add_node(int(i), x=float(x), y=float(y))
    visited = np.zeros_like(sk, bool)
    offs = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    imp = P[..., 1:6]
    lat0 = transform.f + H / 2 * transform.e
    kx = 111_320 * math.cos(math.radians(lat0)) * abs(transform.a)
    ky = 110_570 * abs(transform.e)

    def add_edge(a, b, path):
        if a == b and len(path) < 10:
            return
        pr = np.array(path)
        seg = np.hypot(np.diff(pr[:, 0]) * ky, np.diff(pr[:, 1]) * kx).sum()
        if seg < 1:
            return
        e = imp[pr[:, 0], pr[:, 1]].astype(np.float32).mean(0)
        rank = int(np.argmax(e) + 1)
        keep = pr[::step_px] if len(pr) > 2 * step_px else pr[[0, -1]]
        if not (keep[-1] == pr[-1]).all():
            keep = np.vstack([keep, pr[-1:]])
        coords = tuple(lonlat(r, c) for r, c in keep)
        coords = ((G.nodes[a]["x"], G.nodes[a]["y"]),) + coords[1:-1] + ((G.nodes[b]["x"], G.nodes[b]["y"]),)
        st = float(P[pr[:, 0], pr[:, 1], 6].max()) / 255.0
        d = dict(source_id="unet", road_rank=rank, length_m=float(seg), coords=coords, highway="unclassified",
                 unet_p_structure=st, bridge=st >= 0.5)
        if G.has_edge(a, b) and G.edges[a, b]["length_m"] <= seg:
            return
        G.add_edge(a, b, **d)
        G.add_edge(b, a, **dict(d, coords=tuple(reversed(coords))))

    for r0, c0 in zip(ys, xs):
        a = int(node_of[r0, c0])
        for dr, dc in offs:
            r, c = r0 + dr, c0 + dc
            if not (0 <= r < H and 0 <= c < W) or not sk[r, c] or node_px[r, c] or visited[r, c]:
                continue
            path = [(r0, c0), (r, c)]
            visited[r, c] = True
            pr_, pc_ = r0, c0
            while True:
                nxt = None
                for er, ec in offs:
                    rr, cc = r + er, c + ec
                    if (rr, cc) == (pr_, pc_) or not (0 <= rr < H and 0 <= cc < W) or not sk[rr, cc]:
                        continue
                    if node_px[rr, cc]:
                        if int(node_of[rr, cc]) != a or len(path) > 3:
                            nxt = (rr, cc)
                            break
                        continue
                    if not visited[rr, cc]:
                        nxt = (rr, cc)
                        break
                if nxt is None:
                    break
                path.append(nxt)
                if node_px[nxt]:
                    add_edge(a, int(node_of[nxt]), path)
                    break
                visited[nxt] = True
                pr_, pc_ = r, c
                r, c = nxt
    G.remove_nodes_from([n for n in list(G.nodes) if G.degree(n) == 0])
    return G


# ---------------------------------------------------------------------------
# better tracing: gap bridging, tracing options, the network of a U-Net folder


def bridge_gaps(G: nx.DiGraph, P: np.ndarray, transform, max_m: float = 150.0, cone_deg: float = 50.0, min_p: float = 0.15,
                mean_p: float = 0.3, detour: float = 3.0) -> dict:
    """Join the dead ends of a U-Net network to the road they point at, in place.

    For each dead end, the road points (nodes and edge vertices) within ``max_m`` and within ``cone_deg`` of the direction
    the road arrives from are candidates; a straight link is accepted when the road probability along it stays at or above
    ``min_p`` with a mean of at least ``mean_p``, and when the two ends are not already joined nearby (network distance
    under ``detour`` x the link). The best link (highest mean probability, then shortest) is added; a target inside an
    edge splits it. Links are marked ``unet_bridge``. Returns counts."""
    from scipy.spatial import cKDTree

    H, W = P.shape[:2]
    lat0 = transform.f + H / 2 * transform.e
    kx, ky = 111_320 * math.cos(math.radians(lat0)), 110_570.0

    def m_xy(lonlat):
        a = np.asarray(lonlat, float)
        return np.column_stack([a[:, 0] * kx, a[:, 1] * ky])

    def px_rc(lonlat):
        a = np.asarray(lonlat, float)
        return (a[:, 1] - transform.f) / transform.e - 0.5, (a[:, 0] - transform.c) / transform.a - 0.5

    nbrs = {n: set(G.successors(n)) | set(G.predecessors(n)) for n in G}
    dead = [n for n, s in nbrs.items() if len(s) == 1]
    # candidate points: every vertex of every edge (one direction per pair)
    pairs = [(a, b) for a, b in G.edges if a != b and (a < b or not G.has_edge(b, a))]
    pts, owner, vidx = [], [], []
    for i, (a, b) in enumerate(pairs):
        c = G.edges[a, b]["coords"]
        pts.extend(c)
        owner.extend([i] * len(c))
        vidx.extend(range(len(c)))
    pts_m = m_xy(pts)
    owner, vidx = np.asarray(owner), np.asarray(vidx)
    tree = cKDTree(pts_m)
    Gu = nx.Graph()
    for a, b, d in G.edges(data=True):
        Gu.add_edge(a, b, w=d["length_m"])
    cos_cone = math.cos(math.radians(cone_deg))
    links = []
    for d in dead:
        (nb,) = nbrs[d]
        e = G.edges[nb, d] if G.has_edge(nb, d) else G.edges[d, nb]
        coords = e["coords"] if G.has_edge(nb, d) else tuple(reversed(e["coords"]))
        xy_d = m_xy([coords[-1]])[0]
        back = m_xy([coords[max(0, len(coords) - 4)]])[0]
        v = xy_d - back
        if np.hypot(*v) < 1:
            continue
        v = v / np.hypot(*v)
        idx = tree.query_ball_point(xy_d, max_m)
        if not idx:
            continue
        near = nx.single_source_dijkstra_path_length(Gu, d, cutoff=detour * max_m, weight="w")
        best = None
        for j in idx:
            a, b = pairs[owner[j]]
            if d in (a, b):
                continue
            w = pts_m[j] - xy_d
            L = float(np.hypot(*w))
            if L < 5 or (w @ v) / L < cos_cone:
                continue
            gd = min(near.get(a, np.inf), near.get(b, np.inf))
            if gd < detour * L:
                continue
            lonlat = np.array([coords[-1], pts[j]])
            r, c = px_rc(lonlat)
            n = max(int(np.hypot(r[1] - r[0], c[1] - c[0]) * 2), 2)
            rr = np.clip(np.round(np.linspace(r[0], r[1], n)).astype(int), 0, H - 1)
            cc = np.clip(np.round(np.linspace(c[0], c[1], n)).astype(int), 0, W - 1)
            pv = P[rr, cc, 0] / 255.0
            if pv.min() < min_p or pv.mean() < mean_p:
                continue
            key = (pv.mean(), -L)
            if best is None or key > best[0]:
                imp = P[rr, cc, 1:6].astype(np.float32).mean(0)
                best = (key, owner[j], int(vidx[j]), L, float(pv.mean()), int(np.argmax(imp) + 1))
        if best:
            links.append((d, *best[1:]))
    # split the edges at the target vertices, then add the links
    by_edge: dict = {}
    for d, ei, vi, L, pm, rank in links:
        by_edge.setdefault(ei, set()).add(vi)
    vertex_node: dict = {}
    next_id = max((n for n in G if isinstance(n, (int, np.integer))), default=0) + 1
    split = 0
    for ei, vis in by_edge.items():
        a, b = pairs[ei]
        d0 = dict(G.edges[a, b])
        c = d0["coords"]
        cuts = sorted(v for v in vis if 0 < v < len(c) - 1)
        vertex_node[(ei, 0)], vertex_node[(ei, len(c) - 1)] = a, b
        if not cuts:
            continue
        seg_m = np.hypot(*np.diff(m_xy(c), axis=0).T)
        total = seg_m.sum() or 1.0
        bounds = [0] + cuts + [len(c) - 1]
        nodes = [a]
        for v in cuts:
            G.add_node(next_id, x=float(c[v][0]), y=float(c[v][1]))
            vertex_node[(ei, v)] = next_id
            nodes.append(next_id)
            next_id += 1
        nodes.append(b)
        G.remove_edge(a, b)
        if G.has_edge(b, a):
            G.remove_edge(b, a)
        for (s0, s1), (n0, n1) in zip(zip(bounds[:-1], bounds[1:]), zip(nodes[:-1], nodes[1:])):
            part = tuple(c[s0:s1 + 1])
            dd = dict(d0, coords=part, length_m=float(d0["length_m"] * seg_m[s0:s1].sum() / total))
            G.add_edge(n0, n1, **dd)
            G.add_edge(n1, n0, **dict(dd, coords=tuple(reversed(part))))
        split += 1
    added, link_m = 0, 0.0
    for d, ei, vi, L, pm, rank in links:
        t = vertex_node.get((ei, vi))
        if t is None or t == d or G.has_edge(d, t):
            continue
        cd = ((G.nodes[d]["x"], G.nodes[d]["y"]), (G.nodes[t]["x"], G.nodes[t]["y"]))
        attrs = dict(source_id="unet", road_rank=rank, length_m=float(L), highway="unclassified", bridge=False,
                     unet_p_structure=0.0, unet_bridge=True, unet_bridge_p=round(pm, 3))
        G.add_edge(d, t, coords=cd, **attrs)
        G.add_edge(t, d, coords=tuple(reversed(cd)), **attrs)
        added += 1
        link_m += L
    return {"dead_ends_before": len(dead), "links": added, "edges_split": split,
            "link_km": round(link_m / 1000, 1)}


def parse_tracing(spec: str | dict | None) -> dict:
    """``"low=0.3,bridge=150"`` -> {"low": 0.3, "bridge": 150.0}. Keys: threshold, low, bridge (max link m), bridge_min_p,
    bridge_mean_p, bridge_cone."""
    if not spec:
        return {}
    if isinstance(spec, dict):
        return dict(spec)
    out = {}
    for part in str(spec).split(","):
        k, v = part.split("=", 1)
        out[k.strip()] = float(v)
    return out


def trace_network(P: np.ndarray, transform, tracing: str | dict | None = None) -> tuple[nx.DiGraph, dict]:
    """The U-Net road network with the tracing options of :func:`parse_tracing`; returns the graph and what was done."""
    t = parse_tracing(tracing)
    G = network_from_unet(P, transform, threshold=t.get("threshold", 0.5), low=t.get("low"))
    info = {"tracing": t}
    if t.get("bridge"):
        info["bridging"] = bridge_gaps(G, P, transform, max_m=t["bridge"], cone_deg=t.get("bridge_cone", 50.0),
                                       min_p=t.get("bridge_min_p", 0.15), mean_p=t.get("bridge_mean_p", 0.3))
    return G, info


def unet_network(folder, tracing: str | dict | None = None) -> nx.DiGraph:
    """The road network of a U-Net folder: ``network.pkl`` when the folder holds one (and no tracing is asked), else traced
    from ``unet_oof.tif`` with ``tracing`` (or the folder's ``tracing.json``)."""
    import json
    import pickle
    from pathlib import Path

    folder = Path(folder)
    if tracing is None and (folder / "network.pkl").exists():
        with open(folder / "network.pkl", "rb") as f:
            return pickle.load(f)
    if tracing is None and (folder / "tracing.json").exists():
        tracing = json.loads((folder / "tracing.json").read_text())
    P, tr = load_raster(folder / "unet_oof.tif")
    return trace_network(P, tr, tracing)[0]

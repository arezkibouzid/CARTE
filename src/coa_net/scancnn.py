"""A small convolutional network on image crops, for the two decisions the tabular features cannot see.

Whether a bridge read on the map is a real bridge or a culvert, and whether two roads drawn crossing are a
crossroads or a flyover, shows in how the two symbols overlap: which one is drawn on top, whether the casing of one
is cut by the other, how wide the water is under the road. A crop of the class raster around the candidate keeps
that picture.

* **Input**: a ``SIZE`` x ``SIZE`` pixel crop of the class raster (one channel per colour class) centred on the
  candidate: the middle of a bridge section, or the crossing junction.
* **Network**: three 3x3 convolution blocks (16, 32, 64 channels) with max pooling, global average pooling, one
  output. About 25,000 weights, trained on the CPU in about a minute per fold.
* **Training**: the eight rotations and mirror images of each crop (the map has no preferred direction), class
  weighting for rare positives, and the same spatial folds as :mod:`coa_net.scanlearn`, so every score is on blocks the
  network never saw. The prediction is the mean over the eight orientations.
"""

from __future__ import annotations

import numpy as np

SIZE = 40
N_CLASSES = 9


def crops(classes: np.ndarray, centres_rc: np.ndarray, size: int = SIZE) -> np.ndarray:
    """(n, N_CLASSES, size, size) one-hot crops of the class raster around (row, col) centres (out of sheet = class 0)."""
    h = size // 2
    H, W = classes.shape
    out = np.zeros((len(centres_rc), N_CLASSES, size, size), np.float32)
    for i, (r, c) in enumerate(np.round(centres_rc).astype(int)):
        r0, c0 = r - h, c - h
        a = np.zeros((size, size), np.int64)
        rr0, cc0, rr1, cc1 = max(r0, 0), max(c0, 0), min(r0 + size, H), min(c0 + size, W)
        if rr1 > rr0 and cc1 > cc0:
            a[rr0 - r0:rr1 - r0, cc0 - c0:cc1 - c0] = classes[rr0:rr1, cc0:cc1]
        a = np.clip(a, 0, N_CLASSES - 1)
        out[i][a, np.arange(size)[:, None], np.arange(size)[None, :]] = 1.0
    return out


def _net():
    import torch.nn as nn

    def block(i, o):
        return nn.Sequential(nn.Conv2d(i, o, 3, padding=1), nn.BatchNorm2d(o), nn.ReLU(), nn.MaxPool2d(2))

    return nn.Sequential(block(N_CLASSES, 16), block(16, 32), block(32, 64), nn.AdaptiveAvgPool2d(1), nn.Flatten(),
                         nn.Dropout(0.3), nn.Linear(64, 1))


def _dihedral(x, k: int):
    import torch

    x = torch.rot90(x, k % 4, dims=(2, 3))
    return torch.flip(x, dims=(3,)) if k >= 4 else x


def fit_predict(Xtr: np.ndarray, ytr: np.ndarray, Xte: np.ndarray, epochs: int = 30, seed: int = 0, return_net: bool = False):
    """Train on (Xtr, ytr), return the probability of each crop of Xte (mean over the eight orientations)
    (and the trained network when ``return_net``)."""
    import torch

    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    net = _net()
    pos = max(float(ytr.mean()), 1e-3)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=torch.tensor(min((1 - pos) / pos, 20.0)))
    opt = torch.optim.AdamW(net.parameters(), lr=2e-3, weight_decay=1e-3)
    Xt = torch.from_numpy(Xtr)
    yt = torch.from_numpy(ytr.astype(np.float32))
    n = len(Xt)
    bs = 64
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=3e-3, total_steps=epochs * ((n + bs - 1) // bs))
    for _ in range(epochs):
        net.train()
        perm = rng.permutation(n)
        for b in range(0, n, bs):
            idx = perm[b:b + bs]
            if len(idx) < 2:
                continue
            xb = _dihedral(Xt[idx], int(rng.integers(8)))
            opt.zero_grad()
            loss = loss_fn(net(xb).squeeze(1), yt[idx])
            loss.backward()
            opt.step()
            sched.step()
    net.eval()
    out = np.zeros(len(Xte))
    with torch.no_grad():
        Xe = torch.from_numpy(Xte)
        for k in range(8):
            p = []
            for b in range(0, len(Xe), 256):
                p.append(torch.sigmoid(net(_dihedral(Xe[b:b + 256], k)).squeeze(1)).numpy())
            out += np.concatenate(p) if p else 0.0
    return (out / 8.0, net) if return_net else out / 8.0


def oof(X: np.ndarray, y: np.ndarray, blocks: np.ndarray, folds_of_blocks, n_folds: int = 4, epochs: int = 30, seed: int = 0,
        progress=print) -> np.ndarray:
    """Out-of-fold probabilities on the spatial folds."""
    fold = np.asarray(folds_of_blocks)[blocks]
    p = np.full(len(y), np.nan)
    for f in range(n_folds):
        tr, te = fold != f, fold == f
        if te.sum() == 0 or len(np.unique(y[tr])) < 2:
            continue
        p[te] = fit_predict(X[tr], y[tr], X[te], epochs=epochs, seed=seed + f)
        progress(f"    fold {f}: trained on {int(tr.sum())}, predicted {int(te.sum())}")
    return p


CNN_TASKS = ("bridge_real", "crossing_separated")


def task_crops(ds, task: str, classes, transform, G=None) -> np.ndarray:
    """Crops of the candidates of a corrector task, in the row order of ``scanlearn._task_rows``."""
    from .scanlearn import _task_rows, _to_px

    _, _, _, idx, _ = _task_rows(ds, task)
    if task == "bridge_real":
        mid = np.asarray([g.interpolate(0.5, normalized=True).coords[0] for g in ds["roads"].geometry.values[idx]]).reshape(-1, 2)
    else:
        mid = np.asarray([[G.nodes[c["node"]]["x"], G.nodes[c["node"]]["y"]] for c in ds["cands"]]).reshape(-1, 2)
    return crops(classes, _to_px(transform, mid))


def cross_validate_cnn(ds, cv: dict, G, tif: str, epochs: int = 30, progress=print) -> dict:
    """Replace the tabular out-of-fold probabilities of the two crop tasks by the network's, when it scores
    better on unseen blocks. Returns {task: {"auc_tabular", "auc_cnn", "used"}}; ``cv`` is updated in place."""
    from sklearn.metrics import roc_auc_score

    from .scanlearn import _task_rows, class_raster, decision_scores

    classes, transform = class_raster(tif)
    out = {}
    for task in CNN_TASKS:
        t = cv["tasks"].get(task, {})
        if "_oof" not in t:
            continue
        _, y, b, _, _ = _task_rows(ds, task)
        C = task_crops(ds, task, classes, transform, G)
        p = oof(C, y, b, cv["folds_of_blocks"], epochs=epochs, progress=progress)
        ok = ~np.isnan(p) & ~np.isnan(t["_oof"])
        a_cnn, a_tab = float(roc_auc_score(y[ok], p[ok])), float(roc_auc_score(y[ok], t["_oof"][ok]))
        used = a_cnn > a_tab
        out[task] = {"auc_tabular": a_tab, "auc_cnn": a_cnn, "used": used}
        progress(f"  {task}: crop network AUC {a_cnn:.3f} against tabular {a_tab:.3f} on unseen blocks -> "
                 f"{'network' if used else 'tabular'}")
        if used:
            t["_oof"] = p
            t["auc"] = a_cnn
            t["model"] = "crop network"
    cv["decisions"] = decision_scores(ds, cv)
    cv["cnn"] = out
    return out


def train_final_cnn(ds, G, tif: str, out_dir, tasks, epochs: int = 30) -> None:
    """Train each crop network on every block and save its weights (to correct another sheet)."""
    import torch

    from .scanlearn import _task_rows, class_raster

    classes, transform = class_raster(tif)
    for task in tasks:
        _, y, _, _, _ = _task_rows(ds, task)
        C = task_crops(ds, task, classes, transform, G)
        net = _train(C, y, epochs=epochs)
        torch.save(net.state_dict(), str(out_dir / f"cnn_{task}.pt"))


def _train(X, y, epochs: int = 30, seed: int = 0):
    return fit_predict(X, y, X[:1], epochs=epochs, seed=seed, return_net=True)[1]


def predict_saved(path, X: np.ndarray) -> np.ndarray:
    """Probabilities of a saved crop network (mean over the eight orientations)."""
    import torch

    net = _net()
    net.load_state_dict(torch.load(str(path)))
    net.eval()
    out = np.zeros(len(X))
    with torch.no_grad():
        Xe = torch.from_numpy(X)
        for k in range(8):
            out += np.concatenate([torch.sigmoid(net(_dihedral(Xe[b:b + 256], k)).squeeze(1)).numpy() for b in range(0, len(Xe), 256)]) if len(X) else 0
    return out / 8.0

"""Back end of the AMIAD interface: map tiles, road layers by area, routing, step-by-step extraction, PDF exercises.

Runs on the cluster next to the data and the trained U-Net outputs (configuration: ``sheets.json``). Everything heavy is
prepared once and kept in memory (sheet rasters, traced networks, reference networks), so that a map tile or a route
answers in milliseconds to a second.

    .venv/bin/uvicorn interface.backend.amiad_api:app --port 8008      (or interface/run.sh)
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import pickle
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[2]
CONFIG = json.loads((Path(__file__).with_name("sheets.json")).read_text(encoding="utf-8"))
CACHE = REPO / CONFIG.get("cache_dir", "outputs/interface_cache")
CACHE.mkdir(parents=True, exist_ok=True)
TILE = 256

app = FastAPI(title="AMIAD interface API")
_lock = threading.RLock()
STATUS: dict = {"ready": False, "steps": {}}


def rp(p: str) -> Path:
    return REPO / p


def sheet_cfg(sheet: str) -> dict:
    for s in CONFIG["sheets"]:
        if s["id"] == sheet:
            return s
    raise HTTPException(404, f"unknown sheet {sheet}")


NO_MODEL = "none"  # no segmentation model: routes run directly on OSM or BD TOPO
NO_MODEL_INFO = {"id": NO_MODEL, "label": "Sans modèle de segmentation",
                 "description": "L’image n’est pas lue : l’itinéraire est calculé directement sur OSM (si coché) ou sur la BD TOPO."}


def model_cfg(sheet: str, model: str) -> dict:
    if model == NO_MODEL:
        raise HTTPException(400, "Sans modèle de segmentation : pas de probabilité ni d’extraction. Choisissez un modèle U-Net.")
    for m in sheet_cfg(sheet)["models"]:
        if m["id"] == model:
            return m
    raise HTTPException(404, f"unknown model {model}")


# ----------------------------------------------------------------------------------------------------- U-Net outputs

HF_REPO = os.environ.get("CARTE_HF_REPO") or CONFIG.get("hf_repo")  # published U-Net weights (scripts/publish_hf.py)
_unet_locks: dict[str, threading.Lock] = {}


def unet_dir(sheet: str, model: str) -> Path:
    """Folder of the model's ``unet_oof.tif``. Missing (no release archive): the model's weights are downloaded from
    Hugging Face (``hf`` of the model in sheets.json) and run on the sheet, on the GPU if there is one, else on the CPU
    (about 70 s per sheet, 8 times more with the 8 orientations). Computed once, then read from disk."""
    m = model_cfg(sheet, model)
    d = rp(m["raster"])
    if (d / "unet_oof.tif").exists():
        return d
    with _lock:
        lk = _unet_locks.setdefault(str(d), threading.Lock())
    with lk:
        if (d / "unet_oof.tif").exists():
            return d
        hf = m.get("hf")
        if not (hf and HF_REPO):
            raise HTTPException(503, f"probabilités du modèle {model} absentes ({d}) et pas de poids publiés pour ce modèle")
        step = f"U-Net {model} (poids Hugging Face)"
        STATUS["steps"][step] = "téléchargement"
        try:
            import importlib.util
            from argparse import Namespace

            from huggingface_hub import hf_hub_download

            w = hf_hub_download(HF_REPO, hf["file"])
            ext = Path(os.environ.get("CARTE_EXT", Path.home() / "ext"))
            prc = ext / "ProbRoadClass-DeepLearning"
            if not prc.exists():  # the architecture code (MIT), as scripts/fetch_data.py --prc
                import subprocess

                subprocess.run(["git", "clone", "-q", "https://github.com/DominikM198/ProbRoadClass-DeepLearning.git", str(prc)], check=True)
                subprocess.run(["git", "-C", str(prc), "checkout", "-q", "09e04bb87a306e46acbf37c0df74ba28628365a1"], check=True)
            spec = importlib.util.spec_from_file_location("prc_unet", REPO / "scripts" / "prc_unet.py")
            pu = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(pu)
            STATUS["steps"][step] = "calcul sur la feuille" + (" (8 orientations)" if hf.get("tta") else "")
            t = time.perf_counter()
            pu.infer(Namespace(tif=str(rp(sheet_cfg(sheet)["tif"])), model=w, prc_repo=str(prc), weights=None, dest=str(d),
                               device=os.environ.get("CARTE_UNET_DEVICE"), method="tta" if hf.get("tta") else "base", stride=256))
            (d / "source.json").write_text(json.dumps({"hf_repo": HF_REPO, "file": hf["file"], "tta": bool(hf.get("tta")),
                                                       "note": "final model: the training sheets were seen in training (not out of fold)"},
                                                      indent=1), encoding="utf-8")
            STATUS["steps"][step] = f"prêt ({time.perf_counter() - t:.0f} s)"
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            STATUS["steps"][step] = f"erreur : {e}"
            raise HTTPException(503, f"U-Net {model} : poids Hugging Face indisponibles ou calcul impossible ({e})") from e
    return d


# ----------------------------------------------------------------------------------------------------------- rasters

class Raster:
    """One band of a sheet raster in memory (uint8), its transform and size."""

    def __init__(self, path: Path, band: int = 1):
        import rasterio

        with rasterio.open(path) as src:
            self.a = src.read(band)
            self.tr = src.transform
            self.lut = None
            if src.count == 1 and src.colorinterp[0].name == "palette":
                cm = src.colormap(1)
                lut = np.zeros((256, 3), np.uint8)
                for k, v in cm.items():
                    lut[k] = v[:3]
                self.lut = lut
        self.h, self.w = self.a.shape
        self.bounds = (self.tr.c, self.tr.f + self.h * self.tr.e, self.tr.c + self.w * self.tr.a, self.tr.f)

    def sample(self, lon: np.ndarray, lat: np.ndarray):
        """Nearest pixel for a grid of lon (cols) x lat (rows); returns values and a validity mask."""
        c = np.floor((lon - self.tr.c) / self.tr.a).astype(np.int64)
        r = np.floor((lat - self.tr.f) / self.tr.e).astype(np.int64)
        vr, vc = (r >= 0) & (r < self.h), (c >= 0) & (c < self.w)
        rr, cc = np.clip(r, 0, self.h - 1), np.clip(c, 0, self.w - 1)
        return self.a[np.ix_(rr, cc)], np.outer(vr, vc)


_rasters: OrderedDict = OrderedDict()


def raster(key: str, path: Path, band: int = 1, keep: int = 6) -> Raster:
    with _lock:
        if key in _rasters:
            _rasters.move_to_end(key)
            return _rasters[key]
    r = Raster(path, band)
    with _lock:
        _rasters[key] = r
        while len(_rasters) > keep:
            _rasters.popitem(last=False)
    return r


def centred_raster(sheet: str, model: str) -> Raster:
    """Road score with a 5 x 5 maximum filter: each pixel takes the score of the road centre next to it, so that the
    edges of a sure road (outside the 3 px label band) are not shown as doubtful. Cached on disk."""
    key = f"max5:{sheet}:{model}"
    with _lock:
        if key in _rasters:
            return _rasters[key]
    from scipy.ndimage import maximum_filter

    base = prob_raster(sheet, model)
    f = CACHE / f"max5_{sheet}_{model}.npy"
    if f.exists():
        a = np.load(f)
    else:
        a = maximum_filter(base.a, size=5)
        np.save(f, a)
    r = object.__new__(Raster)
    r.a, r.tr, r.lut, r.h, r.w, r.bounds = a, base.tr, None, base.h, base.w, base.bounds
    with _lock:
        _rasters[key] = r
    return r


def scan_raster(sheet: str) -> Raster:
    return raster(f"scan:{sheet}", rp(sheet_cfg(sheet)["tif"]))


def prob_raster(sheet: str, model: str) -> Raster:
    return raster(f"prob:{sheet}:{model}", unet_dir(sheet, model) / "unet_oof.tif")


@lru_cache(maxsize=16)
def calibration_lut(sheet: str, model: str) -> np.ndarray:
    """Score level (0-255) -> probability of being in the road label band, fitted on the sheet's BD TOPO labels
    (isotonic, as scripts/calibrate.py). Cached on disk."""
    f = CACHE / f"calib_{sheet}_{model}.json"
    if f.exists():
        return np.asarray(json.loads(f.read_text()))
    labels = sheet_cfg(sheet).get("labels")
    if not labels or not rp(labels).exists():
        return np.arange(256) / 255.0
    from sklearn.isotonic import IsotonicRegression

    p = prob_raster(sheet, model).a
    y = np.load(rp(labels), mmap_mode="r")
    n = np.bincount(p.ravel(), minlength=256).astype(float)
    k = np.bincount(p.ravel(), weights=np.asarray(y).ravel().astype(float), minlength=256)
    w = n > 0
    ir = IsotonicRegression(y_min=0, y_max=1, out_of_bounds="clip").fit(np.arange(256)[w], k[w] / n[w], sample_weight=n[w])
    lut = ir.predict(np.arange(256))
    f.write_text(json.dumps([round(float(v), 4) for v in lut]))
    return lut


# ----------------------------------------------------------------------------------------------------------- tiles

def tile_grid(z: int, x: int, y: int):
    n = 2.0 ** z
    lon = (x + (np.arange(TILE) + 0.5) / TILE) / n * 360.0 - 180.0
    ty = y + (np.arange(TILE) + 0.5) / TILE
    lat = np.degrees(np.arctan(np.sinh(np.pi * (1 - 2 * ty / n))))
    return lon, lat


def png(rgba: np.ndarray) -> bytes:
    from PIL import Image

    b = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(b, format="PNG", compress_level=1)
    return b.getvalue()


EMPTY = None
_tiles: OrderedDict = OrderedDict()


def _magma() -> np.ndarray:
    import matplotlib

    return (matplotlib.colormaps["magma_r"](np.linspace(0, 1, 256))[:, :3] * 255).astype(np.uint8)


MAGMA = _magma()


def render_tile(layer: str, sheet: str, z: int, x: int, y: int, model: str, tau: float) -> bytes | None:
    lon, lat = tile_grid(z, x, y)
    rgba = np.zeros((TILE, TILE, 4), np.uint8)
    if layer == "scan":
        r = scan_raster(sheet)
        v, ok = r.sample(lon, lat)
        if not ok.any():
            return None
        rgba[..., :3] = r.lut[v] if r.lut is not None else v[..., None]
        rgba[..., 3] = np.where(ok, 255, 0)
    elif layer == "prob":
        r = prob_raster(sheet, model)
        v, ok = r.sample(lon, lat)
        p = v / 255.0
        m = ok & (p >= 0.08)
        if not m.any():
            return None
        rgba[..., :3] = MAGMA[v]
        rgba[..., 3] = np.where(m, np.clip(p * 1.3, 0, 1) * 210, 0).astype(np.uint8)
    elif layer == "uncert":
        # red: a traced road (score >= 0.5) the model is not sure of (calibrated probability < tau);
        # orange: a possible road it saw but did not trace (score 0.25-0.5); faint green: a sure traced road
        r = centred_raster(sheet, model)
        v, ok = r.sample(lon, lat)
        cal = calibration_lut(sheet, model)[v]
        traced = ok & (v >= 128)
        weak = ok & (v >= 64) & (v < 128)
        doubt = traced & (cal < tau)
        sure = traced & (cal >= tau)
        if not (traced.any() or weak.any()):
            return None
        rgba[doubt] = (215, 48, 31, 225)
        rgba[weak] = (255, 150, 0, 175)
        rgba[sure] = (40, 160, 90, 85)
    else:
        raise HTTPException(404, f"unknown layer {layer}")
    return png(rgba)


@app.get("/api/tiles/{layer}/{z}/{x}/{y}.png")
def tile(layer: str, z: int, x: int, y: int, sheet: str = "hackathon", model: str = "nodice", tau: float = 0.3):
    global EMPTY
    key = (layer, sheet, z, x, y, model if layer != "scan" else "", round(tau, 3) if layer == "uncert" else 0)
    with _lock:
        if key in _tiles:
            _tiles.move_to_end(key)
            data = _tiles[key]
            return Response(data or EMPTY, media_type="image/png", headers={"Cache-Control": "max-age=3600"})
    data = None if (model == NO_MODEL and layer != "scan") else render_tile(layer, sheet, z, x, y, model, tau)
    if EMPTY is None:
        EMPTY = png(np.zeros((TILE, TILE, 4), np.uint8))
    with _lock:
        _tiles[key] = data
        while len(_tiles) > 6000:
            _tiles.popitem(last=False)
    return Response(data or EMPTY, media_type="image/png", headers={"Cache-Control": "max-age=3600"})


# ----------------------------------------------------------------------------------------------------------- vectors

class VectorLayer:
    def __init__(self, gdf, props: list[str], level_col: str | None = None):
        import shapely

        self.gdf = gdf.reset_index(drop=True)
        self.tree = shapely.STRtree(self.gdf.geometry.values)
        self.props = props
        self.level = self.gdf[level_col].values if level_col else np.zeros(len(self.gdf), int)

    def query(self, bbox, max_level: int, tol: float, limit: int = 15000) -> dict:
        import shapely

        idx = self.tree.query(shapely.box(*bbox))
        idx = idx[self.level[idx] <= max_level]
        truncated = len(idx) > limit
        if truncated:
            idx = idx[np.argsort(self.level[idx], kind="stable")[:limit]]
        geoms = shapely.simplify(self.gdf.geometry.values[idx], tol) if tol else self.gdf.geometry.values[idx]
        feats = []
        for i, g in zip(idx, geoms):
            if g.is_empty:
                continue
            props = {p: _clean(self.gdf.at[int(i), p]) for p in self.props}
            feats.append({"type": "Feature", "geometry": shapely.geometry.mapping(g), "properties": props})
        return {"type": "FeatureCollection", "features": feats, "truncated": truncated}


def _clean(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v if isinstance(v, (int, float, str, bool)) else str(v)


_vectors: dict = {}


def bdtopo_layer(sheet: str) -> VectorLayer:
    key = f"bd:{sheet}"
    if key not in _vectors:
        from coa_net.realdata import read_lines

        bd, _ = read_lines(str(rp(sheet_cfg(sheet)["bdtopo"])), "cp1252", ["NATURE", "IMPORTANCE", "NUMERO"])
        bd = bd.to_crs("EPSG:4326")
        imp = bd["IMPORTANCE"].astype(str).str.strip()
        nat = bd["NATURE"].fillna("").str.lower()
        lvl = np.where(imp.isin(["1", "2", "3"]), 0, np.where(imp == "4", 1, np.where(nat.isin(["chemin", "sentier", "escalier", "piste cyclable", "route empierrée"]), 3, 2)))
        bd["lvl"] = lvl
        bd = bd.rename(columns={"NATURE": "nature", "IMPORTANCE": "importance", "NUMERO": "numero"})
        _vectors[key] = VectorLayer(bd, ["nature", "importance", "numero", "lvl"], "lvl")
    return _vectors[key]


def osm_layer(sheet: str) -> VectorLayer:
    key = f"osm:{sheet}"
    if key not in _vectors:
        from coa_net.realdata import read_lines

        o, _ = read_lines(str(rp(sheet_cfg(sheet)["osm"])), "utf-8", ["fclass", "name", "ref", "maxspeed"])
        o = o.to_crs("EPSG:4326")
        f = o["fclass"].fillna("")
        o["lvl"] = np.where(f.str.match(r"^(motorway|trunk|primary|secondary)"), 0, np.where(f.str.match(r"^tertiary"), 1,
                            np.where(f.str.startswith("track") | f.isin(["path", "footway", "cycleway", "steps", "bridleway"]), 3, 2)))
        _vectors[key] = VectorLayer(o, ["fclass", "name", "ref", "maxspeed", "lvl"], "lvl")
    return _vectors[key]


def network_layer(sheet: str, model: str) -> VectorLayer:
    key = f"net:{sheet}:{model}"
    if key not in _vectors:
        import geopandas as gpd
        import shapely

        G = unet_graph(sheet, model)
        r = centred_raster(sheet, model)
        lut = calibration_lut(sheet, model)
        rows, geoms, seen = [], [], set()
        for a, b, d in G.edges(data=True):
            if a == b or (b, a) in seen:
                continue
            seen.add((a, b))
            c = np.asarray(d["coords"])
            geoms.append(shapely.LineString(c))
            rr = np.clip(((c[:, 1] - r.tr.f) / r.tr.e).astype(int), 0, r.h - 1)
            cc = np.clip(((c[:, 0] - r.tr.c) / r.tr.a).astype(int), 0, r.w - 1)
            pc = lut[r.a[rr, cc]]
            rank = int(d.get("road_rank", 5))
            rows.append({"rank": rank, "length_m": round(float(d["length_m"])), "bridge_link": bool(d.get("unet_bridge", False)),
                         "p": round(float(pc.mean()), 3), "p_min": round(float(pc.min()), 3), "lvl": 0 if rank <= 3 else 1 if rank == 4 else 2})
        _vectors[key] = VectorLayer(gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326"), ["rank", "length_m", "bridge_link", "p", "p_min", "lvl"], "lvl")
    return _vectors[key]


@app.get("/api/vectors/{layer}")
def vectors(layer: Literal["bdtopo", "osm", "network"], bbox: str, zoom: float, sheet: str = "hackathon", model: str = "nodice"):
    w, s, e, n = (float(v) for v in bbox.split(","))
    max_level = 0 if zoom < 10 else 1 if zoom < 11.5 else 2 if zoom < 13.5 else 3
    tol = 0.0 if zoom >= 14 else 360.0 / (256 * 2 ** zoom) * 1.0
    if layer == "network" and model == NO_MODEL:
        return JSONResponse({"type": "FeatureCollection", "features": []})
    L = {"bdtopo": lambda: bdtopo_layer(sheet), "osm": lambda: osm_layer(sheet), "network": lambda: network_layer(sheet, model)}[layer]()
    return JSONResponse(L.query((w, s, e, n), max_level, tol))


# ----------------------------------------------------------------------------------------------------------- networks

_graphs: dict = {}
_nets: dict = {}


def unet_graph(sheet: str, model: str):
    """The traced U-Net road network of a model (base tracing p >= 0.5), pickled in the cache after the first time."""
    key = (sheet, model)
    with _lock:
        if key in _graphs:
            return _graphs[key]
    from coa_net import scanunet

    m = model_cfg(sheet, model)
    f = CACHE / f"network_{sheet}_{model}.pkl"
    if f.exists():
        G = pickle.load(open(f, "rb"))
    else:
        G = scanunet.unet_network(unet_dir(sheet, model))
        pickle.dump(G, open(f, "wb"))
    with _lock:
        _graphs[key] = G
    return G


def builtup(sheet: str):
    import geopandas as gpd

    b = sheet_cfg(sheet).get("builtup")
    return gpd.read_file(rp(b)) if b and rp(b).exists() else None


def merged_graph(sheet: str, model: str):
    """U-Net network + the OSM roads it lacks (OSM edges more than 25 m from it), joined at nodes within 30 m."""
    key = (sheet, model, "fill")
    with _lock:
        if key in _graphs:
            return _graphs[key]
    import networkx as nx
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    from coa_net.scenario import load_graph_cached

    G = unet_graph(sheet, model).copy()
    Go, _, _ = load_graph_cached(str(rp(sheet_cfg(sheet)["osm"])), rp(sheet_cfg(sheet)["cache"]), kind="osm")
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    pts = []
    for a, b, d in G.edges(data=True):
        c = np.asarray(d["coords"])
        x, y = tr.transform(c[:, 0], c[:, 1])
        pts.append(np.column_stack([x, y]))
    tree = cKDTree(np.vstack(pts))
    nodes_u = list(G.nodes)
    ux, uy = tr.transform([G.nodes[n]["x"] for n in nodes_u], [G.nodes[n]["y"] for n in nodes_u])
    utree = cKDTree(np.column_stack([ux, uy]))
    mapping = {}
    for n, d in Go.nodes(data=True):
        x, y = tr.transform(d["x"], d["y"])
        dist, j = utree.query([x, y])
        mapping[n] = nodes_u[j] if dist <= 30 else ("osm", n)
    added = 0
    for a, b, d in Go.edges(data=True):
        c = np.asarray(d["coords"])
        x, y = tr.transform(c[:, 0], c[:, 1])
        mid = len(x) // 2
        if tree.query([x[mid], y[mid]])[0] <= 25:
            continue
        ma, mb = mapping[a], mapping[b]
        if ma == mb:
            continue
        for m_, src in ((ma, a), (mb, b)):
            if m_ not in G:
                G.add_node(m_, x=Go.nodes[src]["x"], y=Go.nodes[src]["y"])
        dd = dict(d)
        dd["source_id"] = "osm"
        G.add_edge(ma, mb, **dd)
        added += 1
    G.graph["osm_edges_added"] = added
    with _lock:
        _graphs[key] = G
    return G


def network(sheet: str, model: str, osm_fill: bool):
    key = (sheet, model, osm_fill)
    with _lock:
        if key in _nets:
            return _nets[key]
    from coa_net import pdfexercises as px

    G = merged_graph(sheet, model) if osm_fill else unet_graph(sheet, model)
    N = px.Network(G, f"{model}{' + OSM' if osm_fill else ''}", builtup=builtup(sheet))
    N.base_price = N.price.copy()
    with _lock:
        _nets[key] = N
    return N


def reference_network(sheet: str, which: Literal["bdtopo", "osm"]):
    key = (sheet, which)
    with _lock:
        if key in _nets:
            return _nets[key]
    from coa_net import pdfexercises as px
    from coa_net.scenario import load_graph_cached

    cfg = sheet_cfg(sheet)
    G, _, _ = load_graph_cached(str(rp(cfg[which])), rp(cfg["cache"]), kind="osm" if which == "osm" else "bdtopo")
    N = px.Network(G, "BD TOPO" if which == "bdtopo" else "OSM", priced=False, builtup=builtup(sheet))
    with _lock:
        _nets[key] = N
    return N


# ----------------------------------------------------------------------------------------------------------- routing

LonLat = tuple[float, float]


class RouteRequest(BaseModel):
    sheet: str = "hackathon"
    model: str = "nodice"
    start: LonLat
    end: LonLat
    waypoints: list[LonLat] = Field(default_factory=list)
    waypoint_order: Literal["fixed", "free"] = "fixed"
    objective: Literal["length", "time"] = "length"
    round_trip: bool = False
    speed_cap_kmh: float = 80.0
    area: list[LonLat] | None = None  # deployment area polygon (DA); the sheet when absent
    avoid: list[list[LonLat]] = Field(default_factory=list)  # zones to avoid (RA)
    use_osm: bool = False
    osm_mode: Literal["reference", "fill"] = "reference"
    use_bdtopo: bool = True
    prob_threshold: float | None = None  # calibrated probability; sections below it are penalised
    prob_penalty: float = 3.0
    # probabilistic cost from the U-Net confidence, applied only when the route relies on the TIFF alone
    uncertainty_cost: bool = True
    uncertainty_strength: float = Field(0.5, ge=0, le=10)  # 0.5: knee of the ablation (experiments/INDEX.md)
    explain: bool = True  # segment-by-segment trace (local alternatives: about 0.1 s per segment)
    # mission history: the paths of the previous legs (lon/lat polylines), and what to do with them
    previous_legs: list[list[LonLat]] = Field(default_factory=list)
    history_mode: Literal["forbid", "penalize", "free"] = "forbid"
    history_penalty: float = Field(2.0, ge=0, le=100)


HISTORY_BUFFER_M = 20.0  # a section running within 20 m of an earlier leg on more than half its length is that leg
HISTORY_RELAX_M = (0, 500, 1000, 2000, 5000)


def history_mask(N, legs: list[list[LonLat]]):
    """Sections of the network that an earlier leg used: within 20 m of its path on more than half their length."""
    import numpy as np
    import shapely

    from coa_net.pdfexercises import to_l93

    lines = [shapely.LineString(to_l93(l)) for l in legs if len(l) >= 2]
    m = np.zeros(len(N.edges), bool)
    if not lines:
        return m
    buf = shapely.union_all(lines).buffer(HISTORY_BUFFER_M)
    near = N.tree.query(buf, predicate="intersects")
    if len(near):
        inside = shapely.length(shapely.intersection(N.lines[near], buf)) / np.maximum(shapely.length(N.lines[near]), 1e-6)
        m[near[inside > 0.5]] = True
    return m


def near_points_mask(N, pts: list[LonLat], r_m: float):
    """Sections with both ends within ``r_m`` of one of ``pts`` (the shared point where the new leg starts)."""
    import numpy as np

    from coa_net.pdfexercises import to_l93

    m = np.zeros(len(N.edges), bool)
    for p in pts:
        x, y = to_l93([p])[0]
        du = np.hypot(N.xy[N.u, 0] - x, N.xy[N.u, 1] - y)
        dv = np.hypot(N.xy[N.v, 0] - x, N.xy[N.v, 1] - y)
        m |= (du <= r_m) & (dv <= r_m)
    return m


def make_exercise(req: RouteRequest, sheet_bounds):
    from coa_net.pdfexercises import PdfExercise

    pts = {"PD": tuple(req.start), "PA": tuple(req.end)}
    wps = []
    for i, w in enumerate(req.waypoints, start=1):
        pts[f"WP{i}"] = tuple(w)
        wps.append(f"WP{i}")
    if req.area and len(req.area) >= 3:
        area = [f"DA{i}" for i in range(1, len(req.area) + 1)]
        for k, c in zip(area, req.area):
            pts[k] = tuple(c)
    else:
        w_, s_, e_, n_ = sheet_bounds
        area = ["DA1", "DA2", "DA3", "DA4"]
        for k, c in zip(area, [(w_, n_), (e_, n_), (e_, s_), (w_, s_)]):
            pts[k] = c
    return PdfExercise(number=0, pages=[], points=pts, area=area, start="PD", end="PA", waypoints=wps,
                       waypoint_order=("free" if req.waypoint_order == "free" else "fixed") if wps else "none",
                       objective=req.objective, round_trip_distinct=req.round_trip, speed_cap_kmh=req.speed_cap_kmh,
                       legal_speed=True, requirements=[], outputs=[], description="")


def legs_geojson(res: dict) -> dict:
    import shapely
    from pyproj import Transformer

    tr = Transformer.from_crs("EPSG:2154", "EPSG:4326", always_xy=True)
    feats = []
    for i, l in enumerate(res["legs"]):
        xy = shapely.get_coordinates(l["_line"])
        lon, lat = tr.transform(xy[:, 0], xy[:, 1])
        props = {k: v for k, v in l.items() if k not in ("_line",) and not isinstance(v, (dict, list))}
        props["leg"] = i
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": np.round(np.column_stack([lon, lat]), 6).tolist()},
                      "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def overlap(a, b, tol=25.0, step=10.0) -> float:
    import shapely

    def share(x, y):
        L = shapely.length(x)
        p = shapely.line_interpolate_point(x, np.arange(0, L, step))
        return float(np.mean(shapely.distance(p, y) <= tol)) if len(p) else 0.0

    return (share(a, b) + share(b, a)) / 2


def select_network(req):
    """(network, no_model, base_ref): the U-Net network (optionally with OSM), or OSM / BD TOPO without a model."""
    no_model = req.model == NO_MODEL
    base_ref = None
    if no_model:
        if req.use_osm and sheet_cfg(req.sheet).get("osm"):
            base_ref = "osm"
        elif sheet_cfg(req.sheet).get("bdtopo"):
            base_ref = "bdtopo"
        else:
            raise HTTPException(400, "Sans modèle de segmentation, il faut OSM ou la BD TOPO pour calculer l’itinéraire.")
        N = reference_network(req.sheet, base_ref)
        if not hasattr(N, "base_price"):
            N.base_price = N.price.copy()
    else:
        N = network(req.sheet, req.model, req.use_osm and req.osm_mode == "fill")
    return N, no_model, base_ref


def price_network(req, N, no_model: bool) -> dict:
    """Set N.price for this request (call under ``_lock``): operator threshold penalty, then the uncertainty-aware cost,
    which applies only when the route relies on the TIFF alone (a U-Net network, not completed by OSM)."""
    N.price = N.base_price.copy()
    N.prob_share_below = None
    N.risk = N.confidence = None
    priced = conf_priced = conf_note = None
    tiff_only = not no_model and not (req.use_osm and req.osm_mode == "fill")
    if not no_model and req.prob_threshold is not None and req.prob_threshold > 0:
        owner, pc = calibrated_edge_samples(req.sheet, req.model, N)
        cnt = np.maximum(np.bincount(owner, minlength=len(N.edges)), 1)
        share = np.bincount(owner, weights=(pc < req.prob_threshold).astype(float), minlength=len(N.edges)) / cnt
        if not hasattr(N, "osm_mask"):
            N.osm_mask = np.array([N.G.edges[e].get("source_id") == "osm" for e in N.edges])
        share[N.osm_mask] = 0.0  # OSM roads added to the network are not judged by the U-Net
        N.prob_share_below, N.prob_mean = share, np.bincount(owner, weights=pc, minlength=len(N.edges)) / cnt
        N.price = N.base_price * (1.0 + req.prob_penalty * share)
        d = share > 0
        priced = {"threshold": req.prob_threshold, "strength": req.prob_penalty, "sections_priced": int(d.sum()),
                  "km_below_threshold": round(float((N.length_m * share).sum()) / 2000, 1)}
    if tiff_only and req.uncertainty_cost and req.uncertainty_strength > 0:
        owner, pc = calibrated_edge_samples(req.sheet, req.model, N)
        conf_priced = N.price_by_confidence(owner, pc, req.uncertainty_strength, p_ref=float(calibration_lut(req.sheet, req.model).max()))
    elif req.uncertainty_cost and req.uncertainty_strength > 0:
        conf_note = ("coût d'incertitude non appliqué : " + ("itinéraire calculé sans modèle (OSM / BD TOPO)" if no_model
                     else "le réseau est complété par OSM, l'itinéraire ne repose plus sur la seule image"))
    return {"priced": priced, "conf_priced": conf_priced, "conf_note": conf_note, "tiff_only": tiff_only}


@app.post("/api/route")
def route(req: RouteRequest):
    import shapely

    from coa_net import pdfexercises as px

    t0 = time.perf_counter()
    T = {}
    N, no_model, base_ref = select_network(req)
    T["network"] = time.perf_counter() - t0
    with _lock:
        pr = price_network(req, N, no_model)
        priced, conf_priced, conf_note, tiff_only = pr["priced"], pr["conf_priced"], pr["conf_note"], pr["tiff_only"]
        ex = make_exercise(req, scan_raster(req.sheet).bounds)
        t = time.perf_counter()
        hist, used = None, None
        if req.previous_legs and req.history_mode != "free":
            used = history_mask(N, req.previous_legs)
            hist = {"mode": req.history_mode, "legs": len(req.previous_legs), "km_of_previous_legs": round(float(N.length_m[used].sum()) / 2000, 1)}
        try:
            if hist and req.history_mode == "forbid":
                # the previous paths are forbidden; only if no route exists, they are opened again around the shared point
                shared = [req.start] + [p for p in [*req.waypoints, req.end] if bool(near_points_mask(N, [p], 50.0)[used].any())]
                free = px.solve(ex, N, avoid=req.avoid)  # snapping without the history: a point must not be pushed away by it
                base_snap = {k: v["snap_m"] for k, v in free["points"].items()}
                res = None
                for r in HISTORY_RELAX_M:
                    forbid = used & ~near_points_mask(N, shared, r) if r else used
                    try:
                        cand = px.solve(ex, N, avoid=req.avoid, forbid=forbid)
                    except RuntimeError:
                        continue
                    if all(v["snap_m"] <= base_snap.get(k, 0) + 100 for k, v in cand["points"].items()):
                        res = cand
                        hist["relaxed_m"] = r
                        break
                if res is None:
                    raise RuntimeError("aucun itinéraire sans repasser par les trajets des étapes précédentes, même en les rouvrant "
                                       f"sur {HISTORY_RELAX_M[-1] / 1000:g} km autour du point de départ")
            elif hist and req.history_mode == "penalize":
                base_price = N.price.copy()
                try:
                    N.price = base_price * (1.0 + req.history_penalty * used)
                    res = px.solve(ex, N, avoid=req.avoid)
                finally:
                    N.price = base_price
            else:
                res = px.solve(ex, N, avoid=req.avoid)
        except RuntimeError as e:
            raise HTTPException(422, f"Aucun itinéraire : {e}")
        if hist:
            edges = sorted({e for le in res["_leg_edges"] for e in le})
            hist["km_reused"] = round(float(N.length_m[[e for e in edges if used[e]]].sum()) / 1000, 2)
            if req.history_mode == "forbid":
                res["log"].append(f"mission history: the {hist['km_of_previous_legs']} km of road used by the {hist['legs']} previous leg(s) are "
                                  "forbidden" + (f", reopened only within {hist['relaxed_m']} m of the shared point(s) because no route "
                                                 "existed otherwise" if hist.get("relaxed_m") else "") + f"; reused: {hist['km_reused']} km")
            else:
                res["log"].append(f"mission history: roads of the previous legs cost x{1 + req.history_penalty:g}; reused: {hist['km_reused']} km")
        T["route"] = time.perf_counter() - t
        if conf_priced:
            res["log"].append(f"uncertainty-aware cost (TIFF only): cost x (1 + {req.uncertainty_strength:g} x risk), risk = -ln(calibrated "
                              f"probability / {conf_priced['p_ref']}) averaged along each section")
        elif conf_note:
            res["log"].append(conf_note)
        trace = None
        if req.explain:
            from coa_net.route_explain import explain_route

            t = time.perf_counter()
            trace = explain_route(N, res, req.objective, req.speed_cap_kmh)
            T["explain"] = time.perf_counter() - t
            if hist and trace:
                if req.history_mode == "forbid":
                    trace["criteria"].append(f"trajets des {hist['legs']} étape(s) précédente(s) interdits ({hist['km_of_previous_legs']} km de routes)"
                                             + (f", rouverts seulement à moins de {hist['relaxed_m']} m du point partagé faute d’autre passage"
                                                if hist.get("relaxed_m") else ""))
                else:
                    trace["criteria"].append(f"routes des étapes précédentes pénalisées (coût x{1 + req.history_penalty:g})")
                if hist["km_reused"]:
                    trace["limitations"].append(f"{hist['km_reused']} km repris d’une étape précédente"
                                                + (" (autour du point partagé, seul passage possible)" if req.history_mode == "forbid" else ""))
    out = {"route": legs_geojson(res), "total": res["total"], "log": res["log"], "points": res["points"],
           "objective": req.objective, "network": N.name, "probability_penalty": priced,
           "uncertainty_cost": conf_priced, "uncertainty_cost_note": conf_note, "tiff_only": tiff_only, "explanation": trace, "history": hist,
           "legs": [{k: v for k, v in l.items() if k != "_line"} for l in px.jsonable(res)["legs"]], "references": {}}
    if "visit_order" in res:
        out["visit_order"] = res["visit_order"]
    route_id = f"r{int(time.time() * 1000)}"
    with _lock:
        _routes[route_id] = (req, ex, shapely.union_all([l["_line"] for l in res["legs"]]), res["total"])
        _traces[route_id] = {"request": req.model_dump(), "total": res["total"], "network": N.name, "log": res["log"],
                             "trace": trace, "uncertainty_cost": conf_priced, "legs": out["legs"], "compare": {}}
        while len(_traces) > 50:
            _traces.popitem(last=False)
        while len(_routes) > 50:
            _routes.popitem(last=False)
    out["route_id"] = route_id
    out["compare"] = [w for w, use in (("bdtopo", req.use_bdtopo), ("osm", req.use_osm)) if use and not (no_model and w == base_ref)]
    out["timing_s"] = {k: round(v, 2) for k, v in T.items()} | {"total": round(time.perf_counter() - t0, 2)}
    return JSONResponse(json.loads(json.dumps(out, default=_clean)))


_routes: OrderedDict = OrderedDict()
_traces: OrderedDict = OrderedDict()  # route_id -> what the route chat may say about it


def calibrated_edge_samples(sheet: str, model: str, N):
    """(owner section, calibrated probability) every 20 m along every section of N, cached on N."""
    r = prob_raster(sheet, model)
    owner, p = N.edge_probabilities(r.a[..., None], r.tr)
    if getattr(N, "_pc", None) is None or N._pc[0] is not p:
        N._pc = (p, calibration_lut(sheet, model)[np.clip(np.round(p * 255), 0, 255).astype(int)])
    return owner, N._pc[1]


@app.get("/api/route/{route_id}/compare/{which}")
def compare(route_id: str, which: Literal["bdtopo", "osm"]):
    """The same request solved on BD TOPO or OSM (a check only: it never changes our route)."""
    import shapely

    from coa_net import pdfexercises as px

    if route_id not in _routes:
        raise HTTPException(404, "itinéraire inconnu (recalculez-le)")
    req, ex, ours, total = _routes[route_id]
    t = time.perf_counter()
    R = reference_network(req.sheet, which)
    try:
        rr = px.solve(ex, R, avoid=req.avoid)
    except RuntimeError:
        return {"which": which, "error": "aucun itinéraire sur cette référence"}
    ref = shapely.union_all([l["_line"] for l in rr["legs"]])
    out = {"which": which, "total": rr["total"], "route": legs_geojson(rr),
           "length_gap": total["length_km"] / max(rr["total"]["length_km"], 1e-9) - 1,
           "time_gap": total["duration_min"] / max(rr["total"]["duration_min"], 1e-9) - 1,
           "overlap": overlap(ours, ref), "timing_s": round(time.perf_counter() - t, 2)}
    if route_id in _traces:
        _traces[route_id]["compare"][which] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in out.items() if k != "route"}
    return JSONResponse(json.loads(json.dumps(out, default=_clean)))


@app.get("/api/route/{route_id}/explanation")
def route_explanation(route_id: str):
    if route_id not in _traces:
        raise HTTPException(404, "itinéraire inconnu (recalculez-le)")
    return JSONResponse(json.loads(json.dumps(_traces[route_id], default=_clean)))


class RouteChatRequest(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=60)
    level: Literal["concise", "standard", "detailed"] = "standard"
    order_ids: list[str] = Field(default_factory=list, max_length=40)  # operational orders to take into account
    ui_context: dict | None = None
    use_llm: bool = True  # False: trace answers only (no model)


@app.post("/api/route/{route_id}/chat")
def route_chat(route_id: str, req: RouteChatRequest):
    """Questions about a computed route: answered from the solver's trace, or by the local model on a packed context."""
    from interface.backend import route_chat as RC

    if route_id not in _traces:
        raise HTTPException(404, "itinéraire inconnu (recalculez-le)")
    msgs = [m for m in req.messages if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(422, "la dernière entrée doit être la question de l’utilisateur")
    out = _orders_call(RC.chat, _traces[route_id], msgs, req.level, req.order_ids, req.ui_context, req.use_llm)
    return JSONResponse(json.loads(json.dumps(out, default=_clean)))


# ----------------------------------------------------------------------------------------------------------- convoys

class Convoy(BaseModel):
    name: str = "Convoi"
    start: LonLat
    end: LonLat
    waypoints: list[LonLat] = Field(default_factory=list)
    waypoint_order: Literal["fixed", "free"] = "fixed"
    round_trip: bool = False
    speed_cap_kmh: float = Field(80.0, ge=10, le=130)
    objective: Literal["length", "time"] = "length"
    priority: int = 0  # lower first: earlier convoys keep the best roads


class ConvoysRequest(BaseModel):
    sheet: str = "hackathon"
    model: str = "nodice"
    convoys: list[Convoy] = Field(min_length=1, max_length=64)
    area: list[LonLat] | None = None
    avoid: list[list[LonLat]] = Field(default_factory=list)
    use_osm: bool = False
    osm_mode: Literal["reference", "fill"] = "reference"
    prob_threshold: float | None = None
    prob_penalty: float = 3.0
    uncertainty_cost: bool = True
    uncertainty_strength: float = Field(0.5, ge=0, le=10)  # 0.5: knee of the ablation (experiments/INDEX.md)
    deconflict: Literal["none", "penalize", "forbid"] = "penalize"
    share_penalty: float = Field(2.0, ge=0, le=50)  # sections used by an earlier convoy cost (1 + this) times more


def plan_convoys(req: ConvoysRequest, explain: bool = False) -> dict:
    """Convoys planned one after the other (priority order) on the same priced network; with ``deconflict`` the sections
    an earlier convoy uses (and those running alongside within 20 m) are priced up (``penalize``) or nearly forbidden
    (``forbid``: x 1000, so a convoy still gets a route when no separate one exists, and the sharing is reported)."""
    import shapely

    from coa_net import pdfexercises as px

    t0 = time.perf_counter()
    common = req.model_dump(exclude={"convoys", "deconflict", "share_penalty"})
    first = RouteRequest(**common, start=req.convoys[0].start, end=req.convoys[0].end)
    N, no_model, _ = select_network(first)
    order = sorted(range(len(req.convoys)), key=lambda i: (req.convoys[i].priority, i))
    out, masks, log = [None] * len(req.convoys), {}, []
    with _lock:
        pr = price_network(first, N, no_model)
        base = N.price.copy()
        used = np.zeros(len(N.edges), float)
        factor = {"none": 0.0, "penalize": req.share_penalty, "forbid": 1000.0}[req.deconflict]
        try:
            for i in order:
                c = req.convoys[i]
                rq = RouteRequest(**(common | c.model_dump(exclude={"name", "priority"})))
                N.price = base * (1.0 + factor * used)
                ex = make_exercise(rq, scan_raster(req.sheet).bounds)
                t = time.perf_counter()
                try:
                    res = px.solve(ex, N, avoid=req.avoid)
                except RuntimeError as e:
                    out[i] = {"name": c.name, "error": str(e)}
                    log.append(f"{c.name}: no route ({e})")
                    continue
                edges = sorted({e for le in res["_leg_edges"] for e in le})
                m = res["_solver"].overlap_mask(edges)
                shared = {req.convoys[j].name: round(float(N.length_m[edges][masks[j][edges]].sum()) / 1000, 2) for j in masks}
                masks[i] = m
                used = np.maximum(used, m.astype(float))
                item = {"name": c.name, "index": i, "rank": len(masks), "total": res["total"], "route": legs_geojson(res),
                        "legs": [{k: v for k, v in l.items() if k != "_line"} for l in px.jsonable(res)["legs"]],
                        "visit_order": res.get("visit_order"), "shared_km_with_earlier": shared,
                        "seconds": round(time.perf_counter() - t, 3)}
                if explain:
                    from coa_net.route_explain import explain_route

                    item["explanation"] = explain_route(N, res, rq.objective, rq.speed_cap_kmh)
                out[i] = item
                log.append(f"{c.name} (rank {len(masks)}): {res['total']['length_km']} km, {res['total']['duration_min']} min; "
                           + (f"shares {sum(shared.values()):.1f} km with earlier convoys" if shared else "first convoy"))
        finally:
            N.price = base
    names = [c.name for c in req.convoys]
    ok = [o for o in out if o and "error" not in o]
    return {"convoys": out, "network": N.name, "deconflict": req.deconflict, "share_penalty": req.share_penalty,
            "uncertainty_cost": pr["conf_priced"], "uncertainty_cost_note": pr["conf_note"], "tiff_only": pr["tiff_only"],
            "summary": {"convoys": len(names), "routed": len(ok), "total_km": round(sum(o["total"]["length_km"] for o in ok), 2),
                        "shared_km": round(sum(sum(o["shared_km_with_earlier"].values()) for o in ok), 2),
                        "max_duration_min": max((o["total"]["duration_min"] for o in ok), default=None)},
            "log": log, "timing_s": {"total": round(time.perf_counter() - t0, 2)}}


@app.post("/api/convoys")
def convoys(req: ConvoysRequest):
    return JSONResponse(json.loads(json.dumps(plan_convoys(req), default=_clean)))


# ----------------------------------------------------------------------------------------------------------- scenarios

SCENARIOS = REPO / "outputs" / "scenarios"


class ScenarioDoc(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    scenario: dict  # what the interface holds: points, zones, convoys, options


@app.get("/api/scenarios")
def scenarios_list():
    SCENARIOS.mkdir(parents=True, exist_ok=True)
    out = []
    for f in sorted(SCENARIOS.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({"id": f.stem, "name": d.get("name", f.stem), "saved": d.get("saved")})
        except (OSError, json.JSONDecodeError):
            continue
    return out


@app.post("/api/scenarios")
def scenarios_save(doc: ScenarioDoc):
    """Saved, never overwritten: every save is a new file (name + time)."""
    import re as _re

    SCENARIOS.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sid = f"{_re.sub(r'[^A-Za-z0-9_-]+', '_', doc.name)[:40]}_{stamp}"
    f = SCENARIOS / f"{sid}.json"
    k = 1
    while f.exists():
        f = SCENARIOS / f"{sid}_{k}.json"
        k += 1
    f.write_text(json.dumps({"name": doc.name, "saved": stamp, "scenario": doc.scenario}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"id": f.stem}


@app.get("/api/scenarios/{sid}")
def scenarios_get(sid: str):
    f = SCENARIOS / f"{sid}.json"
    if not f.resolve().is_relative_to(SCENARIOS.resolve()) or not f.exists():
        raise HTTPException(404, "scénario inconnu")
    return json.loads(f.read_text(encoding="utf-8"))


# ----------------------------------------------------------------------------------------------------------- extraction

class ExtractRequest(BaseModel):
    sheet: str = "hackathon"
    model: str = "nodice"
    bbox: tuple[float, float, float, float]  # west, south, east, north
    threshold: float = 0.5
    low: float | None = None
    compare_bdtopo: bool = True


def data_url(rgba: np.ndarray, max_px: int = 1100) -> str:
    from PIL import Image

    im = Image.fromarray(rgba, "RGBA")
    if max(im.size) > max_px:
        s = max_px / max(im.size)
        im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))), Image.NEAREST)
    b = io.BytesIO()
    im.save(b, format="PNG", compress_level=1)
    return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()


@app.post("/api/extract")
def extract(req: ExtractRequest):
    """The extraction of the road network in a rectangle, stage by stage: scan, U-Net probability, road mask,
    centre lines, graph (edges in drawing order)."""
    import networkx as nx
    import rasterio
    from rasterio.windows import from_bounds
    from scipy import ndimage
    from skimage.morphology import skeletonize

    from coa_net import scanunet

    t0 = time.perf_counter()
    T = {}
    m = model_cfg(req.sheet, req.model)
    with rasterio.open(unet_dir(req.sheet, req.model) / "unet_oof.tif") as src:
        win = from_bounds(*req.bbox, src.transform).round_offsets().round_lengths()
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        if win.width * win.height > 4000 * 4000:
            raise HTTPException(413, "Zone trop grande : choisissez un rectangle plus petit (environ 20 x 30 km au plus).")
        if win.width < 8 or win.height < 8:
            raise HTTPException(422, "Zone trop petite ou hors de la carte.")
        P = np.moveaxis(src.read(window=win), 0, -1)
        wtr = src.window_transform(win)
        bounds = rasterio.windows.bounds(win, src.transform)
    sc = scan_raster(req.sheet)
    r0, c0 = int(round((wtr.f - sc.tr.f) / sc.tr.e)), int(round((wtr.c - sc.tr.c) / sc.tr.a))
    A = sc.a[max(r0, 0):r0 + P.shape[0], max(c0, 0):c0 + P.shape[1]]
    T["read"] = time.perf_counter() - t0
    t = time.perf_counter()
    p = P[..., 0]
    img = {}
    rgba = np.zeros(p.shape + (4,), np.uint8)
    rgba[: A.shape[0], : A.shape[1], :3] = sc.lut[A] if sc.lut is not None else A[..., None]
    rgba[..., 3] = 255
    img["scan"] = data_url(rgba)
    heat = np.zeros_like(rgba)
    heat[..., :3] = MAGMA[p]
    heat[..., 3] = np.clip(p / 255.0 * 1.3, 0, 1) * 230
    img["probability"] = data_url(heat)
    thr = int(req.threshold * 255)
    if req.low is not None and req.low < req.threshold:
        ml = p >= int(req.low * 255)
        lab, n = ndimage.label(ml, structure=np.ones((3, 3)))
        peak = ndimage.maximum(p, lab, index=np.arange(1, n + 1)) if n else np.zeros(0)
        mask = np.concatenate([[False], np.asarray(peak) >= thr])[lab]
    else:
        mask = p >= thr
    lab0, _ = ndimage.label(mask, structure=np.ones((3, 3)))
    mask = mask & (np.bincount(lab0.ravel())[lab0] >= 30)
    mk = np.zeros_like(rgba)
    mk[mask] = (31, 111, 235, 200)
    img["mask"] = data_url(mk)
    sk = skeletonize(mask)
    skel = np.zeros_like(rgba)
    skel[ndimage.binary_dilation(sk)] = (130, 80, 223, 255)
    img["skeleton"] = data_url(skel)
    T["images"] = time.perf_counter() - t
    t = time.perf_counter()
    G = scanunet.network_from_unet(P, wtr, threshold=req.threshold, low=req.low)
    T["graph"] = time.perf_counter() - t
    # drawing order: breadth-first from the centre of the largest connected part
    U = nx.Graph()
    U.add_edges_from((a, b) for a, b in G.edges if a != b)
    order, seen = [], set()
    comps = sorted(nx.connected_components(U), key=len, reverse=True)
    cx, cy = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    for comp in comps:
        start = min(comp, key=lambda n: (G.nodes[n]["x"] - cx) ** 2 + (G.nodes[n]["y"] - cy) ** 2)
        for a, b in nx.bfs_edges(U, start):
            order.append((a, b))
    feats, km = [], 0.0
    for k, (a, b) in enumerate(order):
        d = G.edges[a, b] if G.has_edge(a, b) else G.edges[b, a]
        km += d["length_m"] / 1000
        feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": np.round(np.asarray(d["coords"]), 6).tolist()},
                      "properties": {"order": k, "rank": int(d.get("road_rank", 5)), "length_m": round(float(d["length_m"]))}})
    deg = dict(U.degree())
    nodes = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(G.nodes[n]["x"], 6), round(G.nodes[n]["y"], 6)]},
              "properties": {"degree": int(dg), "kind": "dead end" if dg == 1 else "junction" if dg >= 3 else "bend"}}
             for n, dg in deg.items() if dg != 2]
    stats = {"pixels": int(p.size), "road_pixels": int(mask.sum()), "sections": len(order), "km": round(km, 1),
             "junctions": sum(1 for v in deg.values() if v >= 3), "dead_ends": sum(1 for v in deg.values() if v == 1),
             "pieces": len(comps), "largest_piece_share": round(len(comps[0]) / max(len(U), 1), 3) if comps else 0,
             "mean_probability_on_roads": round(float(p[sk].mean() / 255), 3) if sk.any() else None}
    if req.compare_bdtopo:
        t = time.perf_counter()
        stats["bdtopo"] = compare_bdtopo(req.sheet, bounds, feats)
        T["check"] = time.perf_counter() - t
    T["total"] = time.perf_counter() - t0
    return {"bounds": list(bounds), "images": img, "graph": {"type": "FeatureCollection", "features": feats},
            "nodes": {"type": "FeatureCollection", "features": nodes}, "stats": stats, "timing_s": {k: round(v, 2) for k, v in T.items()}}


def compare_bdtopo(sheet: str, bounds, feats) -> dict:
    """Completeness and correctness of the extracted graph against the BD TOPO roads of the rectangle (25 m)."""
    import shapely
    from pyproj import Transformer
    from scipy.spatial import cKDTree

    L = bdtopo_layer(sheet)
    idx = L.tree.query(shapely.box(*bounds))
    idx = idx[L.level[idx] <= 2]
    if not len(idx) or not feats:
        return {"bdtopo_km": 0}
    tr = Transformer.from_crs("EPSG:4326", "EPSG:2154", always_xy=True)
    box = shapely.box(*bounds)

    def sample(geoms):
        pts = []
        for g in geoms:
            g = g.intersection(box)
            for part in getattr(g, "geoms", [g]):
                if part.is_empty or part.length == 0:
                    continue
                c = np.asarray(part.coords)
                x, y = tr.transform(c[:, 0], c[:, 1])
                ln = shapely.LineString(np.column_stack([x, y]))
                pts.append(shapely.get_coordinates(shapely.line_interpolate_point(ln, np.arange(0, ln.length, 10.0))))
        return np.vstack(pts) if pts else np.zeros((0, 2))

    b = sample(L.gdf.geometry.values[idx])
    g = sample([shapely.LineString(f["geometry"]["coordinates"]) for f in feats])
    if not len(b) or not len(g):
        return {"bdtopo_km": round(len(b) * 0.01, 1)}
    db = cKDTree(g).query(b, distance_upper_bound=26)[0]
    dg = cKDTree(b).query(g, distance_upper_bound=26)[0]
    return {"bdtopo_km": round(len(b) * 0.01, 1), "completeness": round(float(np.mean(db <= 25)), 3), "correctness": round(float(np.mean(dg <= 25)), 3)}


# ----------------------------------------------------------------------------------------------------------- PDF exercises

def exercises_json(meta: dict, exs) -> dict:
    out = []
    for e in exs:
        out.append({"number": e.number, "points": {k: list(v) for k, v in e.points.items()}, "area": e.area, "start": e.start, "end": e.end,
                    "waypoints": e.waypoints, "waypoint_order": e.waypoint_order, "objective": e.objective,
                    "round_trip": e.round_trip_distinct, "speed_cap_kmh": e.speed_cap_kmh, "requirements": e.requirements,
                    "description": e.description, "notes": e.notes})
    return {"metadata": meta, "exercises": out}


@app.get("/api/pdf")
def pdf_from_sheet(sheet: str = "hackathon"):
    from coa_net.pdfexercises import read_pdf

    f = sheet_cfg(sheet).get("pdf")
    if not f or not rp(f).exists():
        return {"metadata": {}, "exercises": []}
    return exercises_json(*read_pdf(rp(f)))


class TextOrderRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    sheet: str = "hackathon"
    mode: Literal["command", "auto"] = "command"  # auto: the chat asks; a question comes back as is_command = False
    use_llm: bool = True
    map: dict | None = None  # what is on the map: start, end, waypoints, avoid, area, rect (bbox), pointed (point P)


@app.post("/api/order-text")
def order_text(req: TextOrderRequest):
    """An order written as text -> a scenario patch to preview (named points of the PDF and coordinates only, offline)."""
    from interface.backend import text_order as TO

    return TO.parse(req.text, pdf_from_sheet(req.sheet)["exercises"], mode=req.mode, use_llm=req.use_llm, mapctx=req.map)


@app.post("/api/pdf/upload")
async def pdf_upload(file: UploadFile = File(...)):
    import tempfile

    from coa_net.pdfexercises import read_pdf

    data = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".pdf", dir=CACHE) as tmp:
        tmp.write(data)
        tmp.flush()
        try:
            return exercises_json(*read_pdf(tmp.name))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"PDF illisible : {e}")


# ----------------------------------------------------------------------------------------------------------- sheets, status

@app.get("/api/sheets")
def sheets():
    out = []
    for s in CONFIG["sheets"]:
        b = None
        try:
            import rasterio

            with rasterio.open(rp(s["tif"])) as src:
                b = list(src.bounds)
        except Exception:  # noqa: BLE001
            pass
        out.append({"id": s["id"], "label": s["label"], "bounds": b, "has_bdtopo": bool(s.get("bdtopo")), "has_osm": bool(s.get("osm")),
                    "has_pdf": bool(s.get("pdf")), "models": [{k: m[k] for k in ("id", "label", "description") if k in m} for m in s["models"]]
                    + ([NO_MODEL_INFO] if s.get("osm") or s.get("bdtopo") else []),
                    "default_model": s.get("default_model", s["models"][0]["id"])})
    return out


@app.get("/api/status")
def status():
    return STATUS


def warm_up():
    """Prepare what the first requests need, most useful first."""
    for s in CONFIG["sheets"]:
        sid, dm = s["id"], s.get("default_model", s["models"][0]["id"])
        steps = [("fond de carte", lambda: scan_raster(sid)), ("probabilités", lambda: prob_raster(sid, dm)),
                 ("calibration", lambda: calibration_lut(sid, dm)), ("incertitude", lambda: centred_raster(sid, dm)), ("réseau extrait", lambda: network(sid, dm, False)),
                 ("BD TOPO", lambda: bdtopo_layer(sid)), ("OSM", lambda: osm_layer(sid)),
                 ("réseau extrait (couche)", lambda: network_layer(sid, dm)),
                 ("référence BD TOPO", lambda: reference_network(sid, "bdtopo")), ("référence OSM", lambda: reference_network(sid, "osm")),
                 ("probabilités le long des routes", lambda: network(sid, dm, False).edge_probabilities(prob_raster(sid, dm).a[..., None], prob_raster(sid, dm).tr)),
                 ("réseau + OSM (complément)", lambda: network(sid, dm, True)),
                 ("probabilités (réseau + OSM)", lambda: network(sid, dm, True).edge_probabilities(prob_raster(sid, dm).a[..., None], prob_raster(sid, dm).tr))]
        for name, fn in steps:
            t = time.perf_counter()
            STATUS["steps"][name] = "en cours"
            try:
                fn()
                STATUS["steps"][name] = f"prêt ({time.perf_counter() - t:.0f} s)"
            except Exception as e:  # noqa: BLE001
                STATUS["steps"][name] = f"erreur : {e}"
    STATUS["ready"] = True


# ----------------------------------------------------------------------------------------- constrained itineraries

class ItinerariesRequest(BaseModel):
    sheet: str = "hackathon"
    model: str = "nodice"
    use_osm: bool = False  # without a model: route on OSM (else BD TOPO)
    origin_zone: list[LonLat] | None = None  # polygons; when absent, a disk of zone_radius_km around start / end
    destination_zone: list[LonLat] | None = None
    start: LonLat | None = None
    end: LonLat | None = None
    zone_radius_km: float = Field(8.0, gt=0.2, le=40)
    k: int = Field(3, ge=1, le=6)
    min_sep_km: float = Field(5.0, ge=0, le=50)
    no_intersect: bool = True
    vehicle: str = "pfm_carrier"
    turn_aware: bool = True
    area: list[LonLat] | None = None
    avoid: list[list[LonLat]] = Field(default_factory=list)
    n_seeds: int = Field(20, ge=3, le=40)


@app.get("/api/capabilities")
def capabilities():
    from interface.backend import constrained as C

    return C.capabilities()


@app.post("/api/itineraries")
def itineraries(req: ItinerariesRequest):
    """k distinct itineraries between two zones under the vehicle's constraints (coa_net exercise pipeline)."""
    from interface.backend import constrained as C

    t0 = time.perf_counter()
    cfg = sheet_cfg(req.sheet)
    if req.model == NO_MODEL:
        which = "osm" if req.use_osm and cfg.get("osm") else "bdtopo"
        if not cfg.get(which):
            raise HTTPException(400, "Sans modèle de segmentation, il faut OSM ou la BD TOPO.")

        def graph_fn():
            from coa_net.scenario import load_graph_cached

            G, _, _ = load_graph_cached(str(rp(cfg[which])), rp(cfg["cache"]), kind=which)
            return G
        key, vector, net_name = (req.sheet, which, req.vehicle), True, "OSM" if which == "osm" else "BD TOPO"
    else:
        model_cfg(req.sheet, req.model)
        key, vector, net_name = (req.sheet, req.model, req.vehicle), False, f"réseau lu sur la carte ({req.model})"

        def graph_fn():
            return unet_graph(req.sheet, req.model).copy()
    from coa_net.vehicles import load_catalogue

    if req.vehicle not in load_catalogue():
        raise HTTPException(400, f"véhicule inconnu : {req.vehicle}")

    def zone(poly, pt, name):
        if poly and len(poly) >= 3:
            return C._poly_l93(poly)
        if pt:
            return C._disk_l93(pt, req.zone_radius_km * 1000)
        raise HTTPException(400, f"Dessinez la zone {name} ou placez le point {name}.")

    origin, destination = zone(req.origin_zone, req.start, "de départ"), zone(req.destination_zone, req.end, "d’arrivée")
    cached = key in C._bases
    source = rp(cfg[which]) if req.model == NO_MODEL else unet_dir(req.sheet, req.model) / "unet_oof.tif"
    base = C.get_base(key, graph_fn, lambda: builtup(req.sheet), req.vehicle, vector, disk=CACHE / "bases", source=source)
    t_base = time.perf_counter() - t0
    try:
        out = C.solve(base, origin, destination, req.k, req.min_sep_km, req.no_intersect, req.turn_aware,
                      [C._poly_l93(z) for z in req.avoid if len(z) >= 3],
                      C._poly_l93(req.area) if req.area and len(req.area) >= 3 else None, req.n_seeds)
    except ValueError as e:
        raise HTTPException(422, str(e))
    out["network"] = net_name
    out["vehicle"] = C.VEHICLE_LABELS.get(req.vehicle, req.vehicle)
    out["timing_s"] = {"réseau et véhicule" + (" (en mémoire)" if cached else ""): round(t_base, 1), **out["timing_s"],
                       "total": round(time.perf_counter() - t0, 1)}
    return JSONResponse(json.loads(json.dumps(out, default=_clean)))


# ----------------------------------------------------------------------------------------- operational orders

class OrderChatRequest(BaseModel):
    messages: list[dict] = Field(min_length=1, max_length=40)  # [{role: user|assistant, content}], last = the question
    level: Literal["concise", "standard", "detailed"] = "standard"
    ui_context: dict | None = None  # points, zones and results of the map, sent by the interface
    policy: Literal["lenient", "strict"] = "lenient"


def _orders_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.get("/api/orders")
def orders_list():
    from interface.backend import orders as O

    return {"orders": O.list_orders(), "status": O.llm_status()}


@app.get("/api/orders/status")
def orders_status():
    from interface.backend import orders as O

    return O.llm_status()


@app.get("/api/orders/{order_id}/analysis")
def orders_analysis(order_id: str, level: Literal["concise", "standard", "detailed"] = "standard",
                    policy: Literal["lenient", "strict"] = "lenient", explain: bool = True):
    """Extraction + rules from the RAG + explanations at the requested level (model, or the RAG's wording)."""
    from interface.backend import orders as O

    out = _orders_call(O.explain, order_id, level, policy, use_llm=explain)
    return JSONResponse(json.loads(json.dumps(out, default=_clean)))


@app.post("/api/orders/{order_id}/extract")
def orders_extract(order_id: str):
    """Run the extraction bundle on this order of the corpus (needs the extraction model)."""
    from interface.backend import orders as O

    return _orders_call(O.run_extraction, order_id)


@app.post("/api/orders/upload")
async def orders_upload(file: UploadFile = File(...)):
    """An extraction JSON produced elsewhere (schema 1.0, 2.0 or 3.2)."""
    from interface.backend import orders as O

    data = await file.read()
    if len(data) > 5_000_000:
        raise HTTPException(413, "fichier trop volumineux")
    try:
        obj = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(422, "ce fichier n’est pas un JSON valide")
    return {"id": _orders_call(O.save_upload, obj)}


class OrderLinkRequest(BaseModel):
    from_id: str
    to_id: str
    relation: Literal["amends", "replaces", "cancels", "complements"] | None = None
    action: Literal["add", "reject", "reset"] = "add"


@app.get("/api/orders/links")
def orders_links(ids: str = ""):
    """Links between orders and what is in force: over ``ids`` (comma separated) and their chain, or every link."""
    from interface.backend import order_links as OL

    sel = [i for i in ids.split(",") if i]
    if sel:
        return _orders_call(OL.resolve, sel)
    hs = OL.headers()
    return {"orders": list(hs.values()), "links": OL.links(hs)}


@app.post("/api/orders/links")
def orders_link_set(req: OrderLinkRequest):
    """The operator adds a missed link, rejects a wrong one, or goes back to what was detected."""
    from interface.backend import order_links as OL

    return _orders_call(OL.set_link, req.from_id, req.to_id, req.relation, req.action)


@app.post("/api/orders/{order_id}/chat")
def orders_chat(order_id: str, req: OrderChatRequest):
    from interface.backend import orders as O

    msgs = [m for m in req.messages if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not msgs or msgs[-1]["role"] != "user":
        raise HTTPException(422, "la dernière entrée doit être la question de l’utilisateur")
    return JSONResponse(json.loads(json.dumps(_orders_call(O.chat, order_id, msgs, req.level, req.ui_context, req.policy), default=_clean)))


@app.on_event("startup")
def _startup():
    threading.Thread(target=warm_up, daemon=True).start()


_dist = REPO / "interface" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="front")

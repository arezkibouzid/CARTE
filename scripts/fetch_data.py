"""Downloads the open data and models CARTE needs: nothing private, no API key.

    python scripts/fetch_data.py --prc                    # PRC U-Net code (GitHub, MIT) + pretrained weights (Hugging Face)
    python scripts/fetch_data.py --bdtopo --osm           # roads of the Hackathon sheet: IGN BD TOPO (Etalab 2.0) + OSM (ODbL)
    python scripts/fetch_data.py --llm                    # Qwen3-VL-8B-Instruct (Apache 2.0, 17 GB), optional
    python scripts/fetch_data.py --unet --hf-repo USER/carte-unet   # our fine-tuned U-Nets (Hugging Face, 2 x 61 MB)
    python scripts/fetch_data.py --all --bbox 4.08 48.57 5.77 49.37 --name Hackathon --out examples

What it writes:
- ``EXT/ProbRoadClass-DeepLearning`` (git clone at the commit we used) and ``EXT/prc_weights/prc_binary.pt``
  (``EXT`` = ``~/ext`` by default, as ``scripts/prc_slurm.sh`` expects);
- ``OUT/<name>_BDTopo.shp``: BD TOPO v3 road sections from the IGN Géoplateforme WFS, mapped to the BD TOPO v2 fields the
  code reads (NATURE, IMPORTANCE, POS_SOL, FRANCHISST, SENS, LARGEUR, NB_VOIES, NUMERO, ETAT...), cp1252;
- ``OUT/<name>_OSM.shp``: the Geofabrik free road layer of the regions covering the box, clipped, same fields as the
  hackathon file (osm_id, code, fclass, name, ref, oneway, maxspeed, layer, bridge, tunnel, width);
- ``$AMIAD_MODELS/Qwen3-VL-8B-Instruct`` (``~/extraction/models`` by default) for deploy/serve_llm.sbatch;
- ``outputs/hf/carte_unet_{2sheets,1sheet}.safetensors`` + ``config.json``: our fine-tuned U-Nets, for
  ``scripts/prc_unet.py infer --model ...`` (published with scripts/publish_hf.py).

Not open, so not downloaded: the scanned IGN 1:100,000 sheet and the exercise / order PDFs (handed out at the hackathon),
and our U-Net rasters (in the release archive, see README "Data and models"). Values differ
slightly from the hackathon extracts: today's BD TOPO and OSM, not the snapshot the organisers made.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRC_GIT = "https://github.com/DominikM198/ProbRoadClass-DeepLearning.git"
PRC_COMMIT = "09e04bb87a306e46acbf37c0df74ba28628365a1"
PRC_WEIGHTS = ("https://huggingface.co/DominikM198/ProbRoadClass-DeepLearning/resolve/main/Binary_road_segmentation/"
               "U_Net_Resnet18_Big_Attention_Siegfried_settings_Res_U_Net_ImageNet_Swissmap2.pt")
WFS = "https://data.geopf.fr/wfs/ows"
GEOFABRIK = "https://download.geofabrik.de/europe/france/{}-latest-free.shp.zip"
HACKATHON_BBOX = (4.0809, 48.5756, 5.7624, 49.3668)  # lon0, lat0, lon1, lat1 of the Hackathon sheet
UA = {"User-Agent": "CARTE-fetch-data/1.0"}


def download(url: str, dest: Path):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r, open(tmp, "wb") as f:
        total, done = int(r.headers.get("Content-Length") or 0), 0
        while chunk := r.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            if total:
                print(f"\r  {dest.name}: {done / 1e6:.0f} / {total / 1e6:.0f} MB", end="", flush=True)
    print()
    tmp.replace(dest)


# --------------------------------------------------------------------------------------------------------- PRC

def fetch_prc(ext: Path):
    repo = ext / "ProbRoadClass-DeepLearning"
    if not repo.exists():
        subprocess.run(["git", "clone", PRC_GIT, str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", PRC_COMMIT], check=True)
    w = ext / "prc_weights" / "prc_binary.pt"
    if not w.exists():
        download(PRC_WEIGHTS, w)
    print(f"PRC: {repo} @ {PRC_COMMIT[:7]}, weights {w}")


# --------------------------------------------------------------------------------------------------------- BD TOPO

NATURE_V2 = {"Type autoroutier": "Autoroute", "Rond-point": "Route à 1 chaussée"}
SENS_V2 = {"Double sens": "Double", "Sens direct": "Direct", "Sens inverse": "Inverse"}


def bd_v2(p: dict) -> dict:
    """A BD TOPO v3 road section as the v2 fields of the hackathon extract."""
    raw = str(p.get("position_par_rapport_au_sol") or "0")
    ford = not raw.lstrip("-").isdigit()  # « Gué ou radier »: a ford, as in v2's FRANCHISST
    pos = 0 if ford else int(raw)
    imp = str(p.get("importance") or "NC")
    return {"ID": p.get("cleabs"), "NATURE": NATURE_V2.get(p.get("nature"), p.get("nature")),
            "NUMERO": p.get("cpx_numero") or "NR", "IMPORTANCE": "5" if imp == "6" else imp,
            "CL_ADMIN": p.get("cpx_classement_administratif") or "NR", "FICTIF": "Oui" if p.get("fictif") else "Non",
            "FRANCHISST": "Gué ou radier" if ford else "Pont" if pos > 0 else "Tunnel" if pos < 0 else "NC", "POS_SOL": pos,
            "LARGEUR": float(p.get("largeur_de_chaussee") or 0.0), "NB_VOIES": int(p.get("nombre_de_voies") or 0),
            "SENS": SENS_V2.get(p.get("sens_de_circulation"), "NC"),
            "ETAT": "En construction" if p.get("etat_de_l_objet") == "En construction" else "NR",
            "NOM_ITI": p.get("cpx_toponyme_route_nommee") or None}


def fetch_bdtopo(bbox: tuple, dest: Path, page: int = 5000):
    import geopandas as gpd
    from shapely.geometry import shape

    lon0, lat0, lon1, lat1 = bbox
    rows, geoms, start = [], [], 0
    while True:
        q = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": "BDTOPO_V3:troncon_de_route",
             "BBOX": f"{lat0},{lon0},{lat1},{lon1},urn:ogc:def:crs:EPSG::4326", "SRSNAME": "EPSG:4326",
             "OUTPUTFORMAT": "application/json", "COUNT": page, "STARTINDEX": start, "SORTBY": "cleabs"}
        with urllib.request.urlopen(urllib.request.Request(f"{WFS}?{urllib.parse.urlencode(q)}", headers=UA), timeout=300) as r:
            fc = json.load(r)
        feats = fc.get("features", [])
        for f in feats:
            if f.get("geometry"):
                geoms.append(shape(f["geometry"]))
                rows.append(bd_v2(f["properties"]))
        start += len(feats)
        print(f"\r  BD TOPO: {start} / {fc.get('numberMatched', '?')} sections", end="", flush=True)
        if len(feats) < page:
            break
    print()
    g = gpd.GeoDataFrame(rows, geometry=geoms, crs="EPSG:4326")
    g = g.drop_duplicates("ID")
    dest.parent.mkdir(parents=True, exist_ok=True)
    g.to_file(dest, encoding="cp1252")
    print(f"BD TOPO: {len(g)} sections -> {dest}")


# --------------------------------------------------------------------------------------------------------- OSM

OSM_FIELDS = ["osm_id", "code", "fclass", "name", "ref", "oneway", "maxspeed", "layer", "bridge", "tunnel"]


def fetch_osm(bbox: tuple, dest: Path, regions: list[str], cache: Path):
    import geopandas as gpd
    import pandas as pd

    parts = []
    for reg in regions:
        z = cache / f"{reg}-latest-free.shp.zip"
        if not z.exists():
            download(GEOFABRIK.format(reg), z)
        with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(z) as zf:
            names = [n for n in zf.namelist() if Path(n).stem == "gis_osm_roads_free_1"]
            for n in names:
                zf.extract(n, td)
            shp = next(Path(td).rglob("gis_osm_roads_free_1.shp"))
            parts.append(gpd.read_file(shp, bbox=bbox)[OSM_FIELDS + ["geometry"]])
            print(f"  OSM {reg}: {len(parts[-1])} roads in the box")
    g = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs="EPSG:4326").drop_duplicates("osm_id")
    g = g.clip(bbox)
    g["width"] = float("nan")
    dest.parent.mkdir(parents=True, exist_ok=True)
    g.to_file(dest, encoding="utf-8")
    print(f"OSM: {len(g)} roads -> {dest}")


# --------------------------------------------------------------------------------------------------------- U-Net

def fetch_unet(repo: str, dest: Path):
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        sys.exit('pip install -e ".[hub]" (huggingface_hub, safetensors), then run again')
    for f in ("carte_unet_2sheets.safetensors", "carte_unet_1sheet.safetensors", "config.json", "README.md"):
        p = hf_hub_download(repo, f, local_dir=str(dest))
        print(f"  {p}")
    print(f"U-Net weights -> {dest} (scripts/prc_unet.py infer --model {dest}/carte_unet_2sheets.safetensors ...)")


# --------------------------------------------------------------------------------------------------------- LLM

def fetch_llm(models: Path):
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        sys.exit("pip install huggingface_hub, then run again (or: hf download Qwen/Qwen3-VL-8B-Instruct)")
    d = snapshot_download("Qwen/Qwen3-VL-8B-Instruct", local_dir=str(models / "Qwen3-VL-8B-Instruct"))
    print(f"Qwen3-VL-8B-Instruct -> {d}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="PRC, BD TOPO and OSM (not the language model)")
    ap.add_argument("--prc", action="store_true")
    ap.add_argument("--bdtopo", action="store_true")
    ap.add_argument("--osm", action="store_true")
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--unet", action="store_true", help="our fine-tuned U-Nets from Hugging Face (needs --hf-repo)")
    ap.add_argument("--hf-repo", default=os.environ.get("CARTE_HF_REPO"), help="USER/NAME of the U-Net model repository")
    ap.add_argument("--bbox", type=float, nargs=4, default=HACKATHON_BBOX, metavar=("LON0", "LAT0", "LON1", "LAT1"))
    ap.add_argument("--name", default="Hackathon", help="file prefix: <name>_BDTopo.shp, <name>_OSM.shp")
    ap.add_argument("--out", type=Path, default=REPO / "examples")
    ap.add_argument("--osm-regions", default="champagne-ardenne,lorraine", help="Geofabrik regions of France covering the box")
    ap.add_argument("--ext", type=Path, default=Path(os.environ.get("CARTE_EXT", Path.home() / "ext")))
    ap.add_argument("--models", type=Path, default=Path(os.environ.get("AMIAD_MODELS", Path.home() / "extraction" / "models")))
    a = ap.parse_args()
    if not (a.all or a.prc or a.bdtopo or a.osm or a.llm or a.unet):
        ap.print_help()
        return
    if a.all or a.prc:
        fetch_prc(a.ext)
    if a.all or a.bdtopo:
        fetch_bdtopo(tuple(a.bbox), a.out / f"{a.name}_BDTopo.shp")
    if a.all or a.osm:
        fetch_osm(tuple(a.bbox), a.out / f"{a.name}_OSM.shp", a.osm_regions.split(","), a.ext / "geofabrik")
    if a.llm:
        fetch_llm(a.models)
    if a.unet:
        if not a.hf_repo:
            ap.error("--unet needs --hf-repo USER/NAME (or CARTE_HF_REPO)")
        fetch_unet(a.hf_repo, REPO / "outputs" / "hf")
    print("Not downloaded (not open): the scanned 1:100,000 sheet, the exercise / order PDFs and our U-Net rasters: "
          "take them from the release archive (README, « Data and models »).")


if __name__ == "__main__":
    main()

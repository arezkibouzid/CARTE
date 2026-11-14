"""Builds the release archive: the code at HEAD plus the data and models needed to run CARTE, in one zip.

    .venv/bin/python scripts/make_release.py [--out ~/bouzid/CARTE_release.zip]

Contents (under ``CARTE/``): the committed code (``git archive HEAD``); ``examples/`` (scanned sheet, BD TOPO and OSM
extracts, exercise and order PDFs, second training sheet); the U-Net outputs the app and the figures use (4 networks +
zero-shot + one-sheet), two trained checkpoints, the built-up areas, the calibration and network caches, the extracted
orders and their page images, saved scenarios; the built presentation and the demo video. Left out: environments,
node_modules, logs, the language-model endpoint file (cluster host names) and the training arrays (rebuilt by
``scripts/prc_unet.py prep``).
"""
from __future__ import annotations

import argparse
import io
import subprocess
import tarfile
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRC = "outputs/hackathon100k/prc"
DATA = [
    "examples/Hackathon_100K.tif", "examples/Hackathon_BDTopo.shp", "examples/Hackathon_BDTopo.shx", "examples/Hackathon_BDTopo.dbf",
    "examples/Hackathon_OSM.shp", "examples/Hackathon_OSM.shx", "examples/Hackathon_OSM.dbf", "examples/Hackathon_OSM.cpg",
    "examples/exercise_1-to-10-1.pdf", "examples/orders", "examples/otherdata",
    *[f"{PRC}/{d}" for d in ("mix12k_nodice", "mix12k_nodice_tta", "mix12k_tta", "mix12k", "zeroshot", "finetuned")],
    f"{PRC}/prc_mix12k_final.pt", f"{PRC}/prc_finetuned_final.pt",
    "outputs/hackathon100k/pdf_exercises",
    "outputs/interface_cache/bases", "outputs/scenarios",
    "outputs/orders/extractions", "outputs/orders/pages",
    "docs/presentation/CARTE_presentation.pdf", "docs/presentation/slides.html",
    "docs/presentation/preview", "docs/presentation/img", "docs/presentation/charts", "docs/presentation/demo/CARTE_demo.webm",
    "docs/presentation/demo/demo_chapters.md",
]
CACHE_GLOBS = ["outputs/interface_cache/calib_*.json", "outputs/interface_cache/max5_*.npy", "outputs/interface_cache/network_*.pkl"]
STORED = {".webm", ".png", ".jpg", ".woff2", ".zip", ".pt", ".pdf"}  # already compressed

README = """CARTE — release archive ({date}, commit {commit})

Unzip, then from CARTE/:
  python -m venv .venv && .venv/bin/pip install -e ".[dev]"
  .venv/bin/python scripts/fetch_data.py --prc          # PRC U-Net code and weights (only to run or retrain the U-Net)
  interface/run.sh                                     # http://localhost:8008
  sbatch deploy/serve_llm.sbatch                       # optional local language model (or: scripts/fetch_data.py --llm)

What is here besides the code: examples/ (data handed out at the hackathon: not open, do not redistribute publicly),
outputs/ (our U-Net networks and checkpoints, caches, extracted orders, scenarios), docs/presentation/ (deck PDF, slides,
previews, demo video CARTE_demo.webm). The open data (BD TOPO, OSM, PRC weights, Qwen3-VL) can also be downloaded with
scripts/fetch_data.py. To push the code to GitHub, use the git bundle CARTE_github.bundle (see README.md).
"""


def files(rel: str):
    p = REPO / rel
    if p.is_file():
        yield p
    elif p.is_dir():
        yield from (q for q in sorted(p.rglob("*")) if q.is_file() and "__pycache__" not in q.parts)
    else:
        print("missing:", rel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path.home() / "bouzid" / "CARTE_release.zip")
    a = ap.parse_args()
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    code = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"], capture_output=True, check=True).stdout
    t0, n, size = time.time(), 0, 0
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        with tarfile.open(fileobj=io.BytesIO(code)) as tar:
            for m in tar.getmembers():
                if m.isfile():
                    z.writestr(f"CARTE/{m.name}", tar.extractfile(m).read())
                    n += 1
        extra = [f for rel in DATA for f in files(rel)] + [f for g in CACHE_GLOBS for f in sorted(REPO.glob(g))]
        for f in extra:
            rel = f.relative_to(REPO).as_posix()
            ct = zipfile.ZIP_STORED if f.suffix.lower() in STORED else zipfile.ZIP_DEFLATED
            z.write(f, f"CARTE/{rel}", compress_type=ct)
            n += 1
            size += f.stat().st_size
        z.writestr("CARTE/RELEASE_README.txt", README.format(date=time.strftime("%Y-%m-%d"), commit=commit))
    print(f"{a.out}: {n} files, {size / 1e9:.2f} GB of data + code, zip {a.out.stat().st_size / 1e9:.2f} GB, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()

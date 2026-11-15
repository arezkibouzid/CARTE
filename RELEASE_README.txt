CARTE — release archive (2026-11-14, commit a2155f3)

Unzip, then from CARTE/:
  python -m venv .venv && .venv/bin/pip install -e ".[dev]"
  .venv/bin/python scripts/fetch_data.py --prc          # PRC U-Net code and weights (only to run or retrain the U-Net)
  interface/run.sh                                     # http://localhost:8008
  sbatch deploy/serve_llm.sbatch                       # optional local language model (or: scripts/fetch_data.py --llm)

What is here besides the code: examples/ (data handed out at the hackathon: not open, do not redistribute publicly),
outputs/ (our U-Net networks and checkpoints, caches, extracted orders, scenarios), docs/presentation/ (deck PDF, slides,
previews, demo video CARTE_demo.webm). The open data (BD TOPO, OSM, PRC weights, Qwen3-VL) can also be downloaded with
scripts/fetch_data.py. To push the code to GitHub, use the git bundle CARTE_github.bundle (see README.md).

"""Prepares (and, on request, uploads) the CARTE U-Net weights as a Hugging Face model repository.

    python scripts/publish_hf.py                                   # builds outputs/hf/carte-unet/ only (nothing sent)
    .venv/bin/hf auth login                                        # once, with a token that can write (older hub: huggingface-cli login)
    python scripts/publish_hf.py --repo USER/carte-unet --push     # uploads, PRIVATE repository
    python scripts/publish_hf.py --repo USER/carte-unet --push --public   # uploads, public

What is published: the final fine-tuned networks as safetensors (``carte_unet_2sheets`` = Hackathon + Scenario 1
sheets, 12,000 steps; ``carte_unet_2sheets_nodice``, the same without the Dice loss, the app's default;
``carte_unet_1sheet`` = Hackathon sheet only, 6,000 steps), ``config.json`` (architecture, input,
the 7 outputs) and a model card with the measured results. Not published: the scans, the labels, the U-Net rasters
(derived from the scans). The architecture code is the ProbRoadClass repository (MIT), see the card.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRC_DIR = REPO / "outputs/hackathon100k/prc"
PRC_GIT = "https://github.com/DominikM198/ProbRoadClass-DeepLearning"
PRC_COMMIT = "09e04bb87a306e46acbf37c0df74ba28628365a1"
MODELS = {
    "carte_unet_2sheets": {"source": "prc_mix12k_final.pt", "config": "prc_mix12k_config.json",
                           "trained_on": "Hackathon sheet + Scenario 1 sheet (IGN 1:100,000 scans), BD TOPO roads as labels, half the tiles from each"},
    "carte_unet_2sheets_nodice": {"source": "prc_mix12k_nodice_final.pt", "config": "prc_mix12k_nodice_config.json",
                                  "trained_on": "as carte_unet_2sheets, without the Dice loss (finds more roads: the app's default)"},
    "carte_unet_1sheet": {"source": "prc_finetuned_final.pt", "config": "prc_finetuned_config.json",
                          "trained_on": "Hackathon sheet (IGN 1:100,000 scan), BD TOPO roads as labels"},
}

CARD = """---
license: {license}
library_name: pytorch
pipeline_tag: image-segmentation
tags:
- road-extraction
- historical-maps
- topographic-maps
- semantic-segmentation
- u-net
base_model: DominikM198/ProbRoadClass-DeepLearning
---

# CARTE U-Net: roads from scanned 1:100,000 topographic maps

Part of **CARTE** (Constraint-Aware Routing with Trust Estimation), a convoy route planner. When a sheet has no road
vector data, this network reads the roads from the scanned map, and the planner prices its own doubt: the road
probability enters the route cost.

A fine-tune of the ProbRoadClass Attention Res-U-Net (ResNet-18 encoder, attention gates; Mühlematter et al. 2024,
trained on Swiss Siegfried maps) on French IGN 1:100,000 scans, with a second head for road importance and bridges.

## Files

| file | trained on | steps |
|---|---|---|
| `carte_unet_2sheets.safetensors` | {t2} | {s2} |
| `carte_unet_2sheets_nodice.safetensors` | {t3} | {s3} |
| `carte_unet_1sheet.safetensors` | {t1} | {s1} |

`config.json`: architecture, input, outputs. Each file: about 61 MB, bfloat16 inference on GPU or CPU.

## Input and outputs

- **Input**: RGB scan tile, 512 × 512 px, values / 255 (no other normalisation); the planner predicts with 64 px margins
  (tiles every 384 px), optionally with the 8 rotations / mirror images averaged (TTA).
- **Outputs** (7 channels, logits): 0 road (sigmoid); 1-5 road importance 1..5 as in BD TOPO `IMPORTANCE` (softmax over
  the 5); 6 structure: bridge or section above ground (sigmoid).

## Results (Hackathon sheet, out of fold: every block predicted by a network that never saw it)

| | zero-shot PRC | 1 sheet | 2 sheets |
|---|---|---|---|
| road F1 (pixels, vs BD TOPO) | 0.09 | 0.59 | — |
| network completeness / correctness (within 25 m of BD TOPO roads) | — | 68.6 % / 84.2 % | 69.7 % / 84.0 % |
| main roads found | — | 81.6 % | 82.3 % |
| 10 routing exercises: length gap to the BD TOPO route | — | 7.4 % | 5.1 % |

Two labelled sheets were enough to adapt the network. Training: 1.3 GPU-hours on one H100 for the 4 folds and the final
models; inference about 15 s per sheet on an H100.

## Use

```bash
git clone {prc_git} && git -C ProbRoadClass-DeepLearning checkout {prc_commit}
git clone <CARTE repository> CARTE && cd CARTE && pip install -e ".[real,hub]" torch
python scripts/fetch_data.py --unet --hf-repo {repo}   # downloads the weights
python scripts/prc_unet.py infer --tif SHEET.tif --model outputs/hf/carte_unet_2sheets.safetensors \\
    --prc-repo ../ProbRoadClass-DeepLearning --dest out/ [--device cpu] [--method tta]
```

Writes `out/unet_oof.tif` (7 bands uint8, probabilities x 255, on the scan grid), read by the planner as a road network.

## Limits

- Trained on two IGN 1:100,000 sheets (north-east France): other scales, styles or countries need a few labelled sheets.
- Road labels are BD TOPO sections rasterised on the scan: misregistration and generalisation of the map limit pixel
  scores; paths and tracks were not labelled.
- Uncertain in towns (dense symbols and text): the planner can avoid sections whose probability is low.

## Licence and data

{license_note}

Base network and code: ProbRoadClass-DeepLearning (MIT). Labels: IGN BD TOPO (Licence Ouverte / Etalab 2.0).
Weights SHA-256: {hashes}
"""


def build(out: Path, repo: str, license_: str, license_note: str, prc_repo: str) -> Path:
    import importlib.util

    import torch
    from safetensors.torch import save_file

    spec = importlib.util.spec_from_file_location("prc_unet", REPO / "scripts" / "prc_unet.py")
    pu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pu)
    out.mkdir(parents=True, exist_ok=True)
    hashes, cfgs = {}, {}
    for name, m in MODELS.items():
        net = pu.build(prc_repo, None)
        net.load_state_dict(torch.load(PRC_DIR / m["source"], map_location="cpu"))
        dest = out / f"{name}.safetensors"
        sd = pu.unique_state(net)  # the encoder is reachable under two names: stored once
        save_file(sd, str(dest), metadata={"format": "pt", "source": m["source"], "outputs": "road,imp1..imp5,structure"})
        check = pu.build(prc_repo, None)
        pu.load_weights(check, dest)
        ref, got = net.state_dict(), check.state_dict()
        assert ref.keys() == got.keys() and all(torch.equal(ref[k], got[k]) for k in ref), "round trip"
        hashes[name] = hashlib.sha256(dest.read_bytes()).hexdigest()
        cfgs[name] = json.loads((PRC_DIR / m["config"]).read_text())
        cfgs[name].pop("weights", None)
        print(f"{dest.name}: {len(sd)} tensors, {dest.stat().st_size / 1e6:.1f} MB")
    (out / "config.json").write_text(json.dumps({
        "architecture": "PRC7: ProbRoadClass Resnet18_Unet_Big_Attention(1, imagenet1k=False, p=0.1) + Conv2d(32, 6, 1) head on final1",
        "code": {"prc_repository": PRC_GIT, "prc_commit": PRC_COMMIT, "builder": "scripts/prc_unet.py build()"},
        "input": {"channels": "RGB", "scale": "1/255", "tile": 512, "margin": 64},
        "outputs": ["road (sigmoid)", "importance 1", "importance 2", "importance 3", "importance 4", "importance 5 (softmax over 1-5)",
                    "structure: bridge / above ground (sigmoid)"],
        "training": cfgs, "sha256": hashes}, indent=1), encoding="utf-8")
    t = {n: m["trained_on"] for n, m in MODELS.items()}
    (out / "README.md").write_text(CARD.format(
        license=license_, t2=t["carte_unet_2sheets"], s2=f"{cfgs['carte_unet_2sheets']['steps']:,}", t1=t["carte_unet_1sheet"],
        s1=f"{cfgs['carte_unet_1sheet']['steps']:,}", t3=t["carte_unet_2sheets_nodice"], s3=f"{cfgs['carte_unet_2sheets_nodice']['steps']:,}", prc_git=PRC_GIT, prc_commit=PRC_COMMIT, repo=repo or "USER/carte-unet",
        license_note=license_note, hashes="; ".join(f"`{k}` {v}" for k, v in hashes.items())), encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="arezkibouzid/carte-unet", help="USER/NAME of the model repository")
    ap.add_argument("--out", type=Path, default=REPO / "outputs/hf/carte-unet")
    ap.add_argument("--license", default="mit")
    ap.add_argument("--license-note", default="Weights released under the MIT licence, as the base network. They were "
                    "trained on IGN 1:100,000 scans handed out for the AMIAD hackathon; the scans themselves are not "
                    "included and keep their own terms.")
    ap.add_argument("--prc-repo", default=str(Path.home() / "ext" / "ProbRoadClass-DeepLearning"), help="the PRC code (fetch_data.py --prc)")
    ap.add_argument("--push", action="store_true", help="upload (needs hf auth login or HF_TOKEN)")
    ap.add_argument("--public", action="store_true", help="with --push: make the repository public (default: private)")
    a = ap.parse_args()
    out = build(a.out, a.repo, a.license, a.license_note, a.prc_repo)
    print(f"built {out}")
    if not a.push:
        print("nothing sent (add --repo USER/NAME --push to upload)")
        return
    if not a.repo:
        ap.error("--push needs --repo USER/NAME")
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(a.repo, repo_type="model", private=not a.public, exist_ok=True)
    api.upload_folder(repo_id=a.repo, folder_path=str(out), commit_message="CARTE U-Net weights, config and model card")
    print(f"uploaded to https://huggingface.co/{a.repo} ({'public' if a.public else 'private'})")


if __name__ == "__main__":
    main()

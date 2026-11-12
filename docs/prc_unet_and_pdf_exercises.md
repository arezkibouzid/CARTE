# PRC U-Net, corrector ablation on a second sheet, and the PDF route exercises

*12 November 2026. Sheet `Hackathon_100K.tif` (IGN 1:100 000, lon 4.08-5.76, lat 48.58-49.37: Chalons-en-Champagne,
Verdun, Bar-le-Duc, Saint-Dizier), with its BD TOPO and OSM extracts, and the exercise file `exercise_1-to-10-1.pdf`
(10 route exercises). Everything below is measured out of fold on the 4 x 4 blocks of `scan-learn`: no block is
predicted by a model that saw its labels.*

## 1. What is new

| piece | where | what it does |
| --- | --- | --- |
| PRC U-Net | `scripts/prc_unet.py`, `scripts/prc_slurm.sh` | The Attention Res-U-Net of ProbRoadClass (Muehlematter et al. 2024, github.com/DominikM198/ProbRoadClass-DeepLearning, weights on HuggingFace), zero-shot and fine-tuned out of fold on the GPU with BD TOPO as labels; writes the 7-band raster of `scan-unet`, so `scan-learn --unet` and `network_from_unet` use it unchanged. `finetune --fold -1` trains the model for sheets without labels, `infer` applies it. |
| `coa-net pdf-exercises` | `src/coa_net/pdfexercises.py`, `src/coa_net/cli.py` | Reads an exercise PDF (metadata, deployment-area quadrilateral, PD/PA points, waypoints in fixed or free order, shortest or fastest, strictly different round trip, convoy speed cap) and solves every exercise. Outputs per exercise: `layout.png`, `route.geojson`, `decision.md`, `metrics.json`, the PDF's own figures. |
| `--network prc-alone` | `pdf-exercises` | Solves the exercises on the road network traced from the PRC probabilities (no map reader, no corrector). |
| `--no-osm` | `pdf-exercises`, `scripts/exercise_ablation.py` | Runs without OSM even when the data folder has it. Without BD TOPO the checks are skipped. |
| Ablations | `scripts/scan_ablation.py`, `scripts/exercise_ablation.py` | Corrector ablation on route pairs (table 2.1 of `corrector_route_gate_ablation.md`), and every network on the PDF exercises against BD TOPO and OSM. |
| `scripts/cpu_slurm.sh` | | Runs any `coa-net` command as a Slurm CPU job with the threads pinned to the allocation (seven unpinned jobs on one node thrash: LightGBM and PyTorch each take every core). |

## 2. Fixes found on this sheet

- **Built-up areas.** `builtup_polygons` took the palette indices of the Scenario 1 scan (251, 226, 232). Each IGN sheet has its own palette order: here they are a pale green and two rare tints, and no town was covered. `pdf-exercises` now chooses the indices by colour (`--builtup-rgb`, default 204,206,184, the grey-beige built-up tint; index 217 on this sheet): 2,066 polygons, 527 km2, every town covered.
- **Speed in built-up areas.** A section counted as built-up when its midpoint was inside a polygon; long sections crossing a village were missed. Now the share of each section inside the built-up raster (sampled every 20 m) runs at 50 km/h and the rest at the section's limit.

## 3. Corrector ablation (routes against BD TOPO, 101 town pairs, 150 random pairs)

| variant | white roads to rank 4 | links added | town: time gap | random: time gap | route score, town / random | network within 25 m of BD TOPO |
| --- | --- | --- | --- | --- | --- | --- |
| image reader only | 0 | 0 | +22.4% | +25.4% | 0.393 / 0.405 | 86.3% |
| corrector without U-Net, as gated (nothing passes) | 0 | 0 | +22.5% | +25.4% | 0.393 / 0.405 | 87.0% |
| P + L + W forced on, no U-Net | 1,597 | 274 | +14.4% | +16.4% | 0.266 / 0.291 | 86.3% |
| small U-Net features, as gated (nothing passes) | 0 | 0 | +22.5% | +25.5% | 0.394 / 0.405 | 87.2% |
| **fine-tuned PRC features, as gated (L + W, 0.3 / 0.4)** | 3,803 | 319 | **+10.1%** | **+12.3%** | 0.201 / 0.245 | 87.4% |
| small U-Net alone | | | not routable (33% of pairs reached) | | | 86.6% |
| PRC zero-shot alone | | | not routable (0% of pairs reached) | | | 77.3% |
| **fine-tuned PRC alone** | | | **+9.2%** | **+6.4%** | **0.153 / 0.142** | **88.1%** |

- Unlike Scenario 1, the road-level gate switches the two useful corrections off without good features: only 396 candidate links (63% already real, lift 1.1) and white-road importance at lift 1.46 (gate: 1.5).
- PRC features lift both models over the gate. The small U-Net's do not.
- PRC zero-shot is near chance on IGN symbols (road AUC 0.53); fine-tuned it is the best road detector measured here.

| segmentation network (out of fold) | road AUC | road IoU | importance accuracy | importance 4 vs 5 | structure AUC |
| --- | --- | --- | --- | --- | --- |
| small U-Net (`scan-unet`, CPU, 2,000 steps) | 0.954 | 0.366 | 71.8% | 65.1% | 0.589 |
| PRC zero-shot | 0.526 | 0.048 | - | - | - |
| PRC fine-tuned (6,000 steps, section 6) | 0.960 | 0.418 | 80.3% | 77.1% | 0.655 |

## 4. The 10 PDF exercises: which network is closest to BD TOPO and OSM

Same reading and rules on every network; overlap = symmetric share of route length within 25 m of the reference route.
The 8 exercises every network solves (exercises 5 and 6, two strictly different routes, are solved only by the PRC
networks):

| network | length gap vs BD TOPO | overlap with BD TOPO | length gap vs OSM | overlap with OSM | exercises solved |
| --- | --- | --- | --- | --- | --- |
| **fine-tuned PRC alone** | **7.7%** | **71%** | **8.8%** | **70%** | **10/10** |
| reader + corrector, PRC features | 10.0% | 61% | 11.2% | 60% | 10/10 |
| reader + corrector, no U-Net | 11.2% | 59% | 12.4% | 58% | 8/10 |
| reader + corrector, small U-Net features | 11.2% | 59% | 12.4% | 58% | 8/10 |
| image reader only | 11.2% | 58% | 12.4% | 57% | 8/10 |
| small U-Net alone | 64% | 16% | 66% | 14% | 8/10 |

BD TOPO and OSM themselves overlap 46-99% on the same exercises, so about 70% is near what the references allow.

**Without OSM** (data folder with the scan and BD TOPO only): PRC alone is unchanged (it never used OSM); the
corrector retrained without OSM labels (`scan-learn --unet` on that folder) keeps the same gate decisions (median time
ratio 1.222 -> 1.082, with OSM 1.081) but no longer finds two strictly different routes for exercises 5 and 6.
**PRC alone is the option to use without OSM.**

**Without BD TOPO** (a sheet with no labels): nothing can be trained or scored on that sheet. Train the final model on a
labelled sheet (`prc_unet.py finetune --fold -1`), run `prc_unet.py infer` on the new sheet, then
`pdf-exercises --network prc-alone`. The checks are skipped. The accuracy to expect is the out-of-fold accuracy above;
a second labelled sheet is needed to measure transfer.

## 5. Exercise rules (pdfexercises.py)

- Objective: the description slide decides ("shortest" / "fastest"); the requirement bullets otherwise. Exercises 8 and 10 say "as short as possible" in the bullets and "fastest" on the description: the description is followed and the conflict is written in the decision log.
- Deployment area: the DA1-DA4 quadrilateral; only sections lying entirely inside it are allowed.
- Speed: French default limits from what the map shows (built-up 50, expressway 110, other roads 80), capped at the convoy speed of the PDF (80 km/h). With an 80 km/h cap, fastest and shortest differ only through built-up stretches.
- Free-order waypoints: every order on a matrix of point-to-point costs (24 orders). Strictly different round trip: the second leg may use no section of the first (either direction) nor any section within 20 m of it; both leg orders are tried.
- Doubtful roads (reader network): priced by the corrector's probability; reported lengths and times are never priced.
- Checks only: the same exercise on BD TOPO, the share within 25 m of BD TOPO, the duration with OSM `maxspeed` where tagged.

## 6. PRC fine-tuning

Start: `U_Net_Resnet18_Big_Attention_Siegfried_settings_Res_U_Net_ImageNet_Swissmap2.pt` (ImageNet -> Swiss Map ->
Siegfried), dropout 0.1, pretrained road head kept, a new 1x1 head (importance 1-5, structure) on the last features.
Input RGB / 255 (as PRC). Labels: BD TOPO drawn 3 px wide (road without paths and tracks; `IMPORTANCE`; `POS_SOL` > 0 or
bridge). Tiles 512 px centred in training blocks, one of 8 rotations or mirror images per batch. AdamW (weight decay
1e-4), OneCycle with 10% warm-up, the new head at 10x the learning rate, gradient clip 1.0, bfloat16. Loss: BCE road
(pos weight 3) + Dice road + BCE structure (pos weight 30) + 0.5 CE importance on road pixels; held-out pixels masked.
One H100 per fold.

Baseline (the model behind every number above): 6,000 steps, batch 16, learning rate 3e-4 (head 3e-3), about 450 s per
fold on one H100; final losses 0.72-0.89, still falling. A longer-training sweep (12,000 and 24,000 steps, learning
rate 1e-4, batch 32; held-out fold scored every 3,000 steps with `--eval-every`, two GPUs per configuration) ran
unattended; results in [prc_sweep_results.md](prc_sweep_results.md) (best: **finetuned**, road IoU 0.418).

## 7. Commands

```bash
# PRC: data, zero-shot, fine-tuning out of fold (Slurm array, one fold per GPU), merge
python scripts/prc_unet.py prep --data DATA --out OUT/prc
sbatch scripts/prc_slurm.sh zeroshot OUT/prc && python scripts/prc_unet.py merge --out OUT/prc --name zeroshot
sbatch --array=0-3 scripts/prc_slurm.sh finetune OUT/prc --tag T --steps 6000
python scripts/prc_unet.py merge --out OUT/prc --name T
# corrector with PRC features
coa-net scan-learn --data DATA --unet OUT/prc/T --out OUT/learn_prc
# the 10 exercises: PRC alone (closest to BD TOPO / OSM), or the reader + corrector
coa-net pdf-exercises --data DATA --pdf exercises.pdf --network prc-alone --unet OUT/prc/T --out OUT/ex_prc [--no-osm]
coa-net pdf-exercises --data DATA --pdf exercises.pdf --corrector OUT/learn_prc --out OUT/ex_reader
# a sheet without BD TOPO or OSM: final model on a labelled sheet, inference, exercises
sbatch scripts/prc_slurm.sh finetune OUT/prc --fold -1 --tag T --steps 6000
sbatch scripts/prc_slurm.sh infer OUT/prc --tif NEW/sheet_100K.tif --model OUT/prc/prc_T_final.pt --dest OUT/prc_new
coa-net pdf-exercises --data NEW --pdf exercises.pdf --network prc-alone --unet OUT/prc_new --out OUT/ex_new
# ablations
python scripts/scan_ablation.py --data DATA --cache C --learn OUT/learn_full --learn-unet "PRC=OUT/learn_prc" --alone "PRC=OUT/prc/T" --out OUT/ablation
python scripts/exercise_ablation.py --data DATA --pdf exercises.pdf --cache C --builtup OUT/ex_reader/builtup_areas.geojson \
    --corrector "PRC=OUT/learn_prc" --alone "PRC alone=OUT/prc/T" --out OUT/exercise_ablation [--no-osm]
```

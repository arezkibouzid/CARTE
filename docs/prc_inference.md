# PRC mix12k: training and inference time, hardware, and inference methods

*Sheet `Hackathon_100K.tif` (21,019 x 9,890 px = 207.9 megapixels). Every quality figure is out of fold (each block predicted by the model of the fold that held it out) and measured on the `examples` data only.*

## 1. Hardware and software

| part | used |
| --- | --- |
| GPU (training, inference) | NVIDIA H100 80GB HBM3 (79.2 GB), one per fold; node par2dc5-ai-prd-cl01dgx02; PyTorch 2.14.1+cu130 (CUDA 13.0), bfloat16 autocast |
| CPU (network tracing, routing, exercises) | Intel(R) Xeon(R) Platinum 8480CL, 24 cores allocated (Slurm), node par2dc5-ai-prd-cl01dgx01; Python 3.10.12 |
| cluster | 2 nodes x 8 NVIDIA H100 80 GB (Slurm, partition `low`); at most 2 GPUs per configuration |

## 2. Training of the best run (mix12k: 12,000 steps, batch 16, learning rate 0.0003, tiles 512 px, extra sheets S1 at 50% of the tiles)

| model | node | GPU | steps | training time | incl. held-out scoring and prediction | steps/s | tiles/s | final loss |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| fold 0 | par2dc5-ai-prd-cl01dgx02 | NVIDIA H100 80GB HBM3 | 12,000 | 15.3 min | 15.5 min | 13.1 | 209 | 1.078 |
| fold 1 | par2dc5-ai-prd-cl01dgx02 | NVIDIA H100 80GB HBM3 | 12,000 | 15.4 min | 15.6 min | 13.0 | 208 | 0.887 |
| fold 2 | par2dc5-ai-prd-cl01dgx02 | NVIDIA H100 80GB HBM3 | 12,000 | 15.4 min | 15.6 min | 13.0 | 208 | 0.991 |
| fold 3 | par2dc5-ai-prd-cl01dgx02 | NVIDIA H100 80GB HBM3 | 12,000 | 15.4 min | 15.6 min | 13.0 | 208 | 0.806 |
| final (every block) | par2dc5-ai-prd-cl01dgx01 | NVIDIA H100 80GB HBM3 | 12,000 | 14.5 min | 14.5 min | 13.8 | 221 | 0.854 |

GPU time for the run: 1.28 GPU-hours (4 folds + final model). With 2 GPUs per configuration the 4 folds take 2 rounds; the held-out fold is scored every 3,000 steps (`--eval-every`), which is included in the second time column.

## 3. Inference methods (out of fold, the 4 folds summed)

| method | GPU time | tiles | forward passes | megapixels/s | road AUC | road IoU | importance |
| --- | --- | --- | --- | --- | --- | --- | --- |
| base: 512 px tiles, central 384 px kept, no overlap | 24 s | 1,607 | 1,607 | 8.55 | 0.9658 | 0.4202 | 81.7% |
| TTA: 8 rotations / mirror images averaged | 73 s | 1,663 | 13,304 | 2.83 | 0.9676 | 0.4251 | 82.5% |
| overlap: tiles every 256 px, smooth blending | 88 s | 3,773 | 3,773 | 2.37 | 0.9660 | 0.4204 | 82.2% |
| TTA + overlap | 106 s | 3,773 | 30,184 | 1.97 | 0.9676 | 0.4252 | 82.9% |

Timed with CUDA synchronisation, batch 16, model already loaded (loading the model and the sheet adds about 10-20 s per run).

## 4. Whole sheet, final model (deployment: a sheet without labels)

| method | time for the whole sheet | megapixels/s | tiles | forward passes | GPU |
| --- | --- | --- | --- | --- | --- |
| base: 512 px tiles, central 384 px kept, no overlap | 15 s | 13.97 | - | - | NVIDIA H100 80GB HBM3 |
| TTA: 8 rotations / mirror images averaged | 32 s | 6.48 | 1430 | 11440 | NVIDIA H100 80GB HBM3 |
| overlap: tiles every 256 px, smooth blending | 42 s | 4.93 | 3237 | 3237 | NVIDIA H100 80GB HBM3 |
| TTA + overlap | 59 s | 3.55 | 3237 | 25896 | NVIDIA H100 80GB HBM3 |

## 5. Effect on the reconstructed network, the routes and the uncertainty

| method | network within 25 m of BD TOPO | route score town / random | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM | gap km on the BD TOPO roads not followed | broken-network km (BD TOPO) | uncertainty vs missed road (Spearman) | missed road in the 20% most uncertain cells | gap points in those cells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base: 512 px tiles, central 384 px kept, no overlap | 88.3% | 0.119 / 0.130 | 5.1% / 6.2% | 73.7% / 73.3% | 125.4 | 302.4 | 0.31 | 34.6% (x1.6) | 45% |
| TTA: 8 rotations / mirror images averaged | 89.6% | 0.131 / 0.137 | 5.7% / 6.8% | 73.6% / 74.2% | 123.8 | 298.4 | 0.34 | 35.7% (x1.7) | 48% |
| overlap: tiles every 256 px, smooth blending | 88.4% | 0.120 / 0.132 | 5.1% / 6.2% | 73.6% / 73.1% | 127.8 | 265.1 | 0.32 | 34.9% (x1.6) | 44% |
| TTA + overlap | 89.6% | 0.130 / 0.132 | 5.8% / 6.9% | 75.2% / 75.9% | 121.8 | 259.5 | 0.34 | 35.8% (x1.7) | 50% |

## 6. CPU part of the pipeline (per sheet)

| raster | load | trace the road network | routing network | solve the 10 exercises | network sections | network km |
| --- | --- | --- | --- | --- | --- | --- |
| mix12k_base | 2.7 s | 37.7 s | 2.3 s | 2.7 s | 25,073 | 9,953 |
| mix12k_tta | 2.7 s | 51.3 s | 2.1 s | 2.2 s | 16,093 | 9,651 |
| mix12k_overlap | 2.6 s | 37.3 s | 2.2 s | 2.6 s | 24,725 | 9,943 |
| mix12k_tta_overlap | 2.5 s | 51.8 s | 2.1 s | 2.2 s | 16,104 | 9,650 |


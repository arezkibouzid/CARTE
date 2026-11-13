# Robust training of PRC: connectivity loss and uncertainty-guided sampling (against mix12k)

*Same data and settings as mix12k (Hackathon training folds + Scenario 1, 12,000 steps, batch 16, lr 3e-4); one change per run. Evaluation on the `examples` data only, out of fold, base inference. The uncertain cells come from the out-of-fold uncertainty map of mix12k (label-free), restricted to each fold's training blocks.*

## 1. Pixels (Hackathon sheet, out of fold)

| run | change | road AUC | road IoU | importance | 4 vs 5 | structure AUC |
| --- | --- | --- | --- | --- | --- | --- |
| mix12k | reference (no change) | 0.9658 | 0.4202 | 81.7% | 79.1% | 0.740 |
| mix12k_cld | clDice x0.5 | 0.9650 (-0.001) | 0.4145 (-0.006) | 81.1% | 78.5% | 0.715 |
| mix12k_hard | 50% of the sheet's tiles from the 20% most uncertain cells | 0.9644 (-0.001) | 0.4166 (-0.004) | 81.4% | 78.9% | 0.742 |
| mix12k_cld_hard | clDice x0.5, 50% of the sheet's tiles from the 20% most uncertain cells | 0.9639 (-0.002) | 0.4119 (-0.008) | 81.0% | 78.5% | 0.717 |

## 2. Held-out road IoU during training (mean of the 4 folds)

| run | 3,000 | 6,000 | 9,000 | 12,000 |
| --- | --- | --- | --- | --- |
| mix12k | 0.4124 | 0.4131 | 0.4211 | 0.4204 |
| mix12k_cld | 0.4039 | 0.4096 | 0.4159 | 0.4146 |
| mix12k_hard | 0.4104 | 0.4135 | 0.4172 | 0.4167 |
| mix12k_cld_hard | 0.4064 | 0.4112 | 0.4136 | 0.4120 |

## 3. Training time (one H100 per fold)

| run | fold minutes (4 folds) | steps/s | GPU-hours |
| --- | --- | --- | --- |
| mix12k | 15.5, 15.6, 15.6, 15.6 | 13.0 | 1.04 |
| mix12k_cld | 19.1, 19.3, 19.4, 19.3 | 10.5 | 1.29 |
| mix12k_hard | 15.2, 15.1, 15.5, 15.1 | 13.3 | 1.02 |
| mix12k_cld_hard | 19.3, 19.3, 19.1, 19.3 | 10.5 | 1.28 |

## 4. Reconstructed network, routes, exercises, uncertainty

| run | network within 25 m of BD TOPO | traced sections | route score town / random | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM | gap km (BD TOPO roads not followed) | broken-corridor km | gap points in the 20% most uncertain cells |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| mix12k | 88.3% | 25,098 | 0.119 / 0.131 | 5.1% / 6.2% | 73.7% / 73.2% | 125.3 | 302.0 | 45% |
| mix12k_cld | 89.0% | 14,325 | 0.153 / 0.148 | 6.0% / 7.1% | 75.2% / 76.1% | 139.1 | 219.4 | 45% |
| mix12k_hard | 88.2% | 22,117 | 0.136 / 0.133 | 5.7% / 6.8% | 72.1% / 72.5% | 138.8 | 331.5 | 42% |
| mix12k_cld_hard | 88.7% | 14,322 | 0.147 / 0.137 | 5.9% / 7.0% | 72.2% / 71.3% | 143.6 | 270.3 | 41% |

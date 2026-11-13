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

## 5. Conclusions

- **Neither change improves the routes; mix12k stays the best model.** On pixels all three runs are slightly below it (road IoU 0.412-0.417 against 0.420), and on routes they are worse (route score on town pairs 0.136-0.153 against 0.119; exercises' length gap to BD TOPO 5.7-6.0% against 5.1%).
- **clDice does what it is meant to do on the network, but not on the routes.** The traced network is much cleaner (14,300 sections instead of 25,100: fewer stray fragments), lies closer to BD TOPO (89.0% within 25 m) and has **27% fewer km of broken corridors** (219 against 302); the exercise routes follow the reference corridors more often (overlap 75% / 76% against 74% / 73%). But the gap points on the reference roads not followed rise (139 against 125 km): clDice joins the main corridors and loses minor links, so routes come out about 1 point longer. It costs +24% training time (19.3 instead of 15.5 min per fold).
- **Uncertainty-guided sampling does not help**: drawing half of the sheet's tiles from the 20% most uncertain cells (towns, junctions, dense minor roads) costs accuracy on ordinary roads and worsens every route figure; the gaps do not move to less uncertain places (41-45% of the gap points still in the most uncertain cells).
- **Recommendation**: keep mix12k for routing. clDice is worth keeping in mind when the network itself is the product (cleaner, fewer broken corridors). Training-side changes have now been tried four ways (more steps, a second sheet, TTA at inference, clDice and hard sampling); only the second sheet improved the routes. The remaining gaps are best attacked in the tracing of the network from the probabilities (gap bridging, hysteresis threshold), not in training.
- **Caveat**: these three runs ran on the node with the CUDA 12.8 driver (PyTorch 2.11), mix12k on the CUDA 13 node (PyTorch 2.14); the same code and seeds, but not bit-identical numerics.

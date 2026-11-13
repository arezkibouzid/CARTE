# Experiment 2: does the Dice loss matter? (road loss ablation against mix12k)

*Same data and settings as mix12k (Hackathon training folds + Scenario 1 at 50% of the tiles, 12,000 steps, batch 16, lr 3e-4); only the road loss changes. Evaluation on the `examples` data only, out of fold, base inference and base tracing (p >= 0.5).*

## 1. Pixels and calibration

| run | road loss | road AUC | road IoU | importance | calibration error | share of road among pixels scored 0.4-0.6 | pixels scored >= 0.5 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mix12k | BCE x3 + 1 x Dice | 0.9658 | 0.4202 | 81.7% | 0.019 | 25% | 4.5% |
| mix12k_nodice | BCE x3 | 0.9673 | 0.4056 | 81.8% | 0.026 | 23% | 4.7% |
| mix12k_dice05 | BCE x3 + 0.5 x Dice | 0.9669 | 0.4158 | 81.6% | 0.019 | 24% | 4.6% |
| mix12k_nodice_w1 | BCE x1 | 0.9649 | 0.3518 | 82.0% | 0.002 | 48% | 2.3% |

## 2. Training time

| run | fold minutes | GPU-hours |
| --- | --- | --- |
| mix12k | 15.5, 15.6, 15.6, 15.6 | 1.04 |
| mix12k_nodice | 15.1, 15.2, 15.1, 15.6 | 1.02 |
| mix12k_dice05 | 15.2, 15.2, 15.6, 15.7 | 1.03 |
| mix12k_nodice_w1 | 15.0, 15.2, 15.6, 15.6 | 1.02 |

## 3. Missing roads, routes and the 10 exercises

| network | route score town / random | town time gap | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM | gap km (BD TOPO roads not followed) | of it in stretches over 300 m |
| --- | --- | --- | --- | --- | --- | --- | --- |
| mix12k (BCE x3 + 1 x Dice) | 0.119 / 0.131 | +7.1% | 90% | 5.1% / 6.2% | 73.7% / 73.2% | 125.3 | 111.6 (89%) |
| mix12k_nodice (BCE x3) | 0.096 / 0.124 | +5.5% | 85% | 4.0% / 5.1% | 72.1% / 73.4% | 131.4 | 110.4 (84%) |
| mix12k_dice05 (BCE x3 + 0.5 x Dice) | 0.119 / 0.121 | +7.2% | 90% | 5.8% / 6.9% | 73.0% / 67.4% | 133.6 | 116.0 (87%) |
| mix12k_nodice_w1 (BCE x1) | inf / inf | +36.9% | 4% | 15.5% / 17.9% (8/10) | 21.1% / 19.9% | - | - |

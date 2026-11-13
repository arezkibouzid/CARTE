# Experiment 1: calibrating the U-Net road score (mix12k, out of fold)

*For each fold, an isotonic (monotone) mapping from the 256 score levels to the share of BD TOPO road pixels, fitted on the three other folds (`scripts/calibrate.py`). The calibrated value is the chance that a pixel is in the road label band (BD TOPO centre lines drawn 3 px wide, the training label). The order of the scores is unchanged, so the traced network and the routes are kept exactly (`outputs/hackathon100k/prc/mix12k_cal` reuses the base network); only the tools that read probability values change. Evaluation on the `examples` data only.*

## 1. Reliability before and after

| raw score: score bin | mean value | share that is BD TOPO road | share of pixels |
| --- | --- | --- | --- |
| 0.0-0.1 | 0.00 | 0.5% | 93.19% |
| 0.1-0.2 | 0.14 | 13.0% | 0.96% |
| 0.2-0.3 | 0.25 | 17.2% | 0.57% |
| 0.3-0.4 | 0.35 | 20.0% | 0.42% |
| 0.4-0.5 | 0.45 | 23.5% | 0.40% |
| 0.5-0.6 | 0.55 | 27.1% | 0.38% |
| 0.6-0.7 | 0.65 | 30.4% | 0.42% |
| 0.7-0.8 | 0.75 | 34.8% | 0.45% |
| 0.8-0.9 | 0.86 | 42.1% | 0.76% |
| 0.9-1.0 | 0.95 | 62.7% | 2.46% |
| expected calibration error | 0.019 | | |

| calibrated: score bin | mean value | share that is BD TOPO road | share of pixels |
| --- | --- | --- | --- |
| 0.0-0.1 | 0.00 | 0.5% | 92.93% |
| 0.1-0.2 | 0.14 | 14.4% | 1.95% |
| 0.2-0.3 | 0.25 | 24.6% | 1.19% |
| 0.3-0.4 | 0.35 | 34.7% | 0.94% |
| 0.4-0.5 | 0.45 | 44.8% | 0.70% |
| 0.5-0.6 | 0.55 | 55.1% | 0.71% |
| 0.6-0.7 | 0.65 | 64.8% | 1.08% |
| 0.7-0.8 | 0.73 | 73.5% | 0.49% |
| 0.8-0.9 | 0.80 | 82.3% | 0.00% |
| 0.9-1.0 | 1.00 | 100.0% | 0.00% |
| expected calibration error | 0.000 | | |

The raw tracing threshold 0.5 is a calibrated 0.25, 0.25, 0.25, 0.25 (folds 0-3): a pixel traced as road has about a 25% chance of lying in the 3 px label band. The calibrated values stop near 0.75, because the map draws roads wider than the label band: even a sure road pixel is in the band about 3 times out of 4.

## 2. Bridging with honest thresholds (bridges added to the base network, max 150 m)

| bridging | links | km | on a BD TOPO road | at least partly | no road | OSM confirms |
| --- | --- | --- | --- | --- | --- | --- |
| raw scores: min 0.15, mean 0.3 (earlier) | 2,176 | 224.9 | 31% | 60% | 28% | 36% |
| calibrated: min 0.15, mean 0.3 | 960 | 106.2 | 38% | 74% | 14% | 42% |
| calibrated: min 0.3, mean 0.5 | - | | | | | |
| calibrated: min 0.4, mean 0.6 | - | | | | | |

## 3. Routes with the calibrated bridges

| network | route score town / random | town time gap | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM |
| --- | --- | --- | --- | --- | --- |
| base (no bridges) | 0.119 / 0.131 | +7.1% | 90% | 5.1% / 6.2% | 73.7% / 73.2% |
| calibrated min 0.15 / mean 0.3 | 0.116 / 0.132 | +7.0% | 90% | 5.1% / 6.2% | 73.7% / 73.3% |
| calibrated min 0.3 / mean 0.5 | 0.119 / 0.131 | +7.1% | 90% | 5.1% / 6.2% | 73.7% / 73.2% |
| calibrated min 0.4 / mean 0.6 | 0.119 / 0.131 | +7.1% | 90% | 5.1% / 6.2% | 73.7% / 73.2% |

## 4. Uncertainty map and the operator's threshold on the calibrated scale

| measure | raw score | calibrated |
| --- | --- | --- |
| Spearman, uncertainty vs share of BD TOPO road missed | 0.31 | -0.20 |
| BD TOPO road missed in the 20% most uncertain cells (lift) | 34.6% (x1.6) | 42.8% (x0.9) |
| exercise gap points in those cells | 45% | 20% |

Operator's threshold on the calibrated probability (sections below it cost 1 + 3 x the share below):

| threshold (calibrated) | network km below it | length gap vs BD TOPO | overlap with BD TOPO | length gap vs OSM | overlap with OSM |
| --- | --- | --- | --- | --- | --- |
| no penalty | 0 | 5.1% | 73.7% | 6.2% | 73.2% |
| 0.2 | 0 | 5.1% | 73.7% | 6.2% | 73.2% |
| 0.3 | 178 | 5.3% | 75.5% | 6.5% | 75.0% |
| 0.4 | 582 | 6.0% | 74.2% | 7.1% | 73.4% |
| 0.5 | 899 | 6.5% | 65.1% | 7.6% | 58.2% |
| 0.6 | 1,584 | 11.0% | 36.7% | 12.1% | 31.4% |

## 5. Conclusions

- **The calibration works**: the out-of-fold isotonic mapping turns the score into an honest probability (calibration error 0.019 -> 0.000; calibrated 0.45 -> 44.8% road, 0.55 -> 55.1%). The raw tracing threshold 0.5 is a calibrated 0.25 in every fold, and calibrated values stop near 0.75 because the map draws roads wider than the 3 px label band.
- **Bridging is not supported by the model**: with honest thresholds (a 30% chance or more along the whole link) no link survives at all; the loosest calibrated rule (min 0.15, mean 0.3) keeps 960 links instead of 2,176, more of them real (38% on a BD TOPO road instead of 31%; 14% crossing no road instead of 28%), with the same small route effect (route score 0.116 against 0.119 without bridges) and no effect on the exercises. The base tracing stays the recommendation.
- **The uncertainty map must stay on the raw score**: on the calibrated scale the entropy is highest on sure road pixels (calibrated 0.5-0.75, inside or at the edge of the label band) and stops pointing at missed roads (Spearman +0.31 -> -0.20; 45% -> 20% of the exercise gap points in the most uncertain cells). The entropy of a "pixel in the 3 px band" probability does not measure "is there a road here".
- **The operator's threshold is better read on the calibrated scale**: threshold 0.3 = "less than a 30% chance of being a road" gives the best routes (overlap 75.5% with BD TOPO, 75.0% with OSM, for +0.2 point of length), like 0.7 on the raw score; from 0.5 up it penalises much of the real network (58-65% overlap at 0.5, 31-37% at 0.6).
- **Use**: `coa-net pdf-exercises --network prc-alone --unet outputs/hackathon100k/prc/mix12k_cal --prob-threshold 0.3` (same network and routes as mix12k without the threshold; the threshold now reads as a real chance).

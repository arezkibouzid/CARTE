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

## 4. Fair comparison (added after the automatic run)

**Plain BCE x1 traced at 0.25.** Its scores are nearly calibrated, so the 0.5 tracing threshold keeps only 2.3% of the pixels and the network falls apart (4% of the pairs reached). Traced at 0.25, which is the raw 0.5 of the other runs on the calibrated scale (`docs/prc_calibration.md`), it is a fair comparison (`outputs/hackathon100k/retrace/nodice_w1_t025`).

**Route pairs reached by every network** (101 town pairs, 110 random pairs), so that a more fragmented network is not judged on pairs snapped far away:

| road loss | town: time gap | town: score | random: time gap | random: score | pairs reached alone |
| --- | --- | --- | --- | --- | --- |
| BCE x3 + Dice (mix12k) | +7.1% | 0.119 | +5.1% | 0.112 | 90% |
| **BCE x3, no Dice** | **+5.5%** | **0.096** | +4.6% | 0.113 | 85% |
| BCE x3 + 0.5 x Dice | +7.2% | 0.119 | +4.9% | 0.107 | 90% |
| BCE x1, traced at 0.25 | +6.6% | 0.116 | +6.4% | 0.143 | 89% |

**The 10 exercises and missing roads:**

| road loss | length gap BD TOPO / OSM | overlap BD TOPO / OSM | gap km | of it in stretches over 300 m | long stretches with p < 0.15 |
| --- | --- | --- | --- | --- | --- |
| BCE x3 + Dice (mix12k) | 5.1% / 6.2% | 73.7% / 73.2% | 125.3 | 111.6 | 51% |
| **BCE x3, no Dice** | **4.0% / 5.1%** | 72.1% / 73.4% | 131.4 | 110.4 | **28%** |
| BCE x3 + 0.5 x Dice | 5.8% / 6.9% | 73.0% / 67.4% | 133.6 | 116.0 | 47% |
| BCE x1, traced at 0.25 | 4.5% / 5.6% | 70.4% / 72.2% | 130.9 | 110.3 | 61% |

Per exercise without Dice (length gap to BD TOPO): +4.5%, +4.5%, +8.9%, +8.1%, +4.0%, +4.2%, +0.6%, +0.2%, +2.6%, +2.4% (with Dice: +5.5%, +5.5%, +10.3%, +9.3%, +4.9%, +3.1%, +2.1%, +1.8%, +4.4%, +4.2%): closer on 9 of 10; the worst snap of a point is smaller (1,077 m against 1,521 m).

## 5. Conclusions: is Dice worth keeping?

- **Not for routing.** Without Dice, the exercise routes are the closest to BD TOPO and OSM of every model trained (length gap 4.0% / 5.1% against 5.1% / 6.2%; exercises 7-8 within 1% of BD TOPO), and town-pair routes improve by 19% (score 0.119 -> 0.096) on the pairs every network reaches; random pairs are unchanged (0.112 -> 0.113).
- **Dice does not reduce missing roads**, contrary to what was expected: about 110 km of long stretches stay untraced with or without it. Without Dice the U-Net is less blind on them (only 28% of the long stretches below p 0.15 instead of 51%), but they stay under the tracing threshold.
- **The cost of dropping Dice**: road IoU 0.420 -> 0.406 (the thresholded pixels match the 3 px label band less), and a slightly more fragmented network (85% of the pairs reached instead of 90%, losing some rural random pairs). Calibration is not better (error 0.026).
- **Half Dice brings nothing**, and **plain BCE without the road weight** gives near-calibrated scores but, even traced fairly at 0.25, a noisy network (45,000 sections against 25,000) with weaker rural routes: the weight of 3 on road pixels matters more than Dice.
- **Recommendation**: for routing, use **mix12k_nodice** (`outputs/hackathon100k/prc/mix12k_nodice`, base tracing). Keep mix12k when the pixel map itself is the product (IoU) or when the network must reach the most points. The final model of mix12k_nodice (every block + Scenario 1, for sheets without labels) is not trained yet (about 15 minutes on one H100).

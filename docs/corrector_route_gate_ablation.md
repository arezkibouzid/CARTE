# Learned corrector, fourth version: route gate, crop network, displacement, U-Net, and the ablation of every step

*10 November 2026, branch `scan-roads`. Scenario 1 data, image network loader v7, corrector from `coa-net scan-learn` applied out of fold: no block is corrected by a model that saw its labels. Earlier versions: `docs/corrector_route_impact.md` (9 Nov), `docs/corrector_pricing_results.md` (10 Nov, pricing), `docs/corrector_closer_to_bdtopo.md` (10 Nov, links and graph features). BD TOPO and OSM are the labels and the yardstick; they never steer a route.*

## 1. What is new

### 1.1 Route gate (`src/coa_net/scanroute.py`)

The road-level gate asks whether the changes a correction makes are right. The route gate asks whether they make the routes closer to BD TOPO's.

- **Pairs.** 60 random pairs of points per fold, 10 to 70 km apart, with both ends in blocks of the same fold. That gives 240 pairs, and every pair is corrected only by models that never saw its blocks.
- **Routes.** The fastest convoy route between each pair: 60 km/h cap, roads of rank 5 or better, doubtful links priced as the planner prices them. It is computed on the corrected image network and on BD TOPO.
- **Score.** The mean |log(time ratio)| plus the mean |log(length ratio)| against BD TOPO. It is 0 when the image routes match BD TOPO's exactly. A route made too short counts as much as one made too long.
- **Thresholds.** The threshold of each addition is tuned on the route score. Any threshold where fewer than half the changes are right on unseen blocks is skipped, whatever the routes say.
- **Keeping a correction.** Each correction is then removed in turn. It is kept only when the score is worse without it, both overall and on at least 3 of the 4 folds.

Result on Scenario 1 (`corrector_report.md`, section "Route gate"):

| correction | threshold chosen | changes on unseen blocks | right | score with | score without | folds where it helps | kept |
| --- | --- | --- | --- | --- | --- | --- | --- |
| add a missing link | p ≥ 0.3 (road gate: 0.5) | 1,413 links | 70% | 0.287 | 0.373 | 4 of 4 | yes |
| white road to rank 4 | p ≥ 0.5 (road gate: 0.7) | 3,300 roads, 1,673 km | 55% | 0.287 | 0.328 | 4 of 4 | yes |

- **The route gate picks lower thresholds than the road gate.** A missing link costs a long detour, and a wrong link costs little because it is priced. A white road wrongly moved to rank 4 costs a few minutes, while one left at rank 5 makes every route through it 50% slower.
- **The cost is road-level precision.** 55% of the white roads moved to rank 4 are importance 4 in BD TOPO. At the road gate's threshold it was 68%. The 50% floor stops the gate from going further.

### 1.2 Crop network for bridges and flyovers (`src/coa_net/scancnn.py`, PyTorch on the CPU)

- **Input.** A 40 x 40 pixel crop of the class raster around each candidate, one channel per colour class: the middle of a bridge section, or the crossing junction.
- **Network.** Three convolution blocks (16, 32 and 64 channels), global pooling, about 25,000 weights.
- **Training.**
  - The eight rotations and mirror images of each crop are used in training and averaged at prediction.
  - Rare positives are weighted.
  - It uses the same spatial folds as the tabular models, about a minute per fold.
- **Results on unseen blocks:**

| decision | examples | AUC, tabular | AUC, crop network |
| --- | --- | --- | --- |
| two roads drawn crossing are grade-separated | 2,785 (45 positive) | 0.65 | **0.88** |
| a bridge read on the map is a BD TOPO bridge | 646 | 0.59 | **0.65** |

- **The network sees the overlap.** Its probabilities replace the tabular ones for both tasks.
- **Its decisions still fail the gate.**
  - **Flyovers.** With 45 true flyovers in 2,785 crossings, no threshold gets above 30% precision.
  - **Bridge to culvert.** 82-84% of these changes are right, but the base rate is already 72%. That lift of 1.1 is too low.
  - **On routes.** Switching both decisions on anyway makes the town-pair gap 1.5 points worse (table 2.1, "full + CNN decisions").
- **So both stay off.** A better use is listed in section 4.
- **Without PyTorch.** `scan-learn` runs without it (`--no-cnn`, or torch not installed). Install it with `pip install -e .[cnn]`.

### 1.3 Learned displacement (`src/coa_net/scangeom.py`)

- **Samples.** Points every 50 m along each image road, at least 40 m from its ends, so junctions never move.
- **Target.** The signed offset along the line's normal to the nearest parallel BD TOPO road within 60 m.
- **Features** (image only): the classes on each side at 0-8, 8-16 and 16-32 pixels, the nearest other image line on each side and its symbol, the width, the curvature, and the distance to the nearer end.
- **Model.** LightGBM regression, scored out of fold.
- **Moving the lines.** The predicted offsets are smoothed along the line, tapered to zero at its ends and capped at 40 m.

| | before | after (unseen blocks) |
| --- | --- | --- |
| mean distance to BD TOPO at the sample points | 11.4 m | 10.7 m |
| sample points within 10 m | 54% | 57% |
| image network within 25 m of BD TOPO | 86.3% | **87.5%** |
| R² of the offset | | 0.09 |

- **Only the drawing moves.** The first version also recomputed each road's length from the moved vertices. That added about 1% of wiggle to every road and made routes 0.5 points further from BD TOPO. Now `length_m` is kept, so routes are unchanged by construction, and the map checks, itineraries and 25 m scores use the moved lines.
- **The gain is real but small.** The model explains only 9% of the displacement. Most of it depends on the generalisation choices of the map maker, which the neighbourhood does not fully show.

### 1.4 U-Net on the scan (`src/coa_net/scanunet.py`, `coa-net scan-unet`, PyTorch on the CPU)

A segmentation network looks at the picture itself rather than at the reader's decisions. It can see a road continue under an expressway band or a label, and a bridge drawn across another road.

- **Input.** The scan's colours (RGB from the palette), in tiles of 192 x 192 pixels.
- **Labels.** BD TOPO drawn onto the scan grid, 3 pixels wide (about 20 m).
- **Three outputs per pixel:**

| output | classes | what it means | label |
| --- | --- | --- | --- |
| road | 2: road / no road | a road a convoy can use | every BD TOPO road except tracks, stone tracks, paths, cycle tracks and steps (8.3% of the pixels) |
| importance | 5: importance 1 to 5 | 1 expressway, 2 main road, 3 regional road, 4 good local road, 5 minor local road | BD TOPO `IMPORTANCE`, scored on road pixels only |
| structure | 2: on a structure / not | a road on a bridge or an overpass | BD TOPO section above ground (`POS_SOL` > 0, or `FRANCHISST` = bridge), 0.02% of the pixels |

- **Network.** A 4-level U-Net, 12 to 96 channels, 0.27 M weights, 7 output channels (1 + 5 + 1).
- **Loss.** Binary cross-entropy for road and structure (positives weighted 3 and 30), cross-entropy for importance on road pixels.
- **Training.** 2,000 steps of 8 tiles per fold, with random rotations and mirror images. About 12 minutes per fold on 4 CPU cores, and 1 hour for the 4 folds plus a final network on every block.
- **Honest scores.** The same 4 folds as the corrector. Each fold's network is trained with the held-out blocks masked out of the loss, and predicts only those blocks. Every pixel's probability therefore comes from a network that never saw its block.

| pixel score on unseen blocks | value |
| --- | --- |
| road: AUC | 0.912 |
| road: IoU at p ≥ 0.5 | 0.40 (the 3-pixel label sits 1-2 pixels from the drawn symbol, which makes IoU harsh) |
| importance: accuracy | 80.7% |
| importance 4 against 5: accuracy | 79.9% (about the share of importance 5, so no skill per pixel) |
| structure: AUC | 0.830 |

**Two uses.**
1. **As corrector features** (`scan-learn --unet DIR`). The road, importance and structure probabilities are sampled along every road, along every candidate link's straight line, around every crossing, and across each line for the displacement. The corrector, the route gate and the ablation then run as before.
2. **Alone, as its own network.** The road probability is thresholded at 0.5 and thinned to centre lines. The lines are traced into a graph, ranked by the importance output, and a bridge is set where the structure output reaches 0.5. There is no reader, no corrector and no pricing. On Scenario 1 this gives 24,300 km of road, with 473 bridges.

**What the features change in the corrector** (AUC on unseen blocks):

| task | without the U-Net | with the U-Net features | most useful U-Net feature |
| --- | --- | --- | --- |
| road is real | 0.778 | **0.788** | mean road probability along the line (20% of the gain) |
| white road is importance 4 | 0.696 | **0.735** | probability of importance 1-4 (14%) |
| missing link is real | 0.742 | **0.788** | lowest road probability along the link (14%) |
| crossing is grade-separated (tabular) | 0.647 | 0.690 | structure probability around the junction (17% + 12%); the crop CNN (0.876) is still better |
| bridge is a BD TOPO bridge (tabular) | 0.590 | 0.630 | the crop CNN (0.649) is still better |
| displacement, R² | 0.09 | **0.15** | where the road probability peaks across the line; network within 25 m 87.5% -> 87.9% |

- **At the road gate's thresholds, the corrections get more precise.**
  - Links: 1,269 added, 77% right (74% before).
  - White roads to rank 4: 997 roads, 70% right (748 at 68% before).
- **The route gate now picks p ≥ 0.4 for white roads.** That moves 6,651 roads to rank 4 with 51% right, just above the 50% floor.

## 2. Ablation

Every variant applies the same out-of-fold predictions. Only the corrections switched on and their thresholds change.

- **P** = pricing of doubtful roads and label gaps, by the corrector's probability (1 + 3 (1 - p) for a gap, 1 + 3 (0.7 - p) for a road below 0.7).
- **W** = white roads to rank 4.
- **L** = missing links added.
- **G** = lines moved by the learned displacement.

Every variant except "none" includes the pricing.

### 2.1 Routes against BD TOPO (105 town pairs, 127 random pairs 15-70 km apart)

Fastest convoy routes, 60 km/h cap, rank 5 or better. The gap is the total time (or median length) of the image routes over BD TOPO's. The route score is the one of section 1.1. The random pairs are drawn with another seed than the route gate's pairs, so they are a fresh check.

| variant | white roads to rank 4 | links added | town pairs: time gap | town pairs: length gap | random pairs: time gap | random pairs: length gap | route score, town / random | route km within 25 m of BD TOPO (town) | network within 25 m of BD TOPO |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| none | 0 | 0 | +23.7% | +14.6% | +25.0% | +14.9% | 0.373 / 0.382 | 88.8% | 86.3% |
| P | 0 | 0 | +23.7% | +14.6% | +25.0% | +15.7% | 0.373 / 0.383 | 88.9% | 86.3% |
| P + removals | 0 | 0 | +23.7% | +14.6% | +25.0% | +15.7% | 0.373 / 0.383 | 88.9% | 86.5% |
| P + W, old features (9 Nov) | 101 | 0 | +23.8% | +14.6% | +25.0% | +14.9% | 0.372 / 0.382 | 89.0% | 86.3% |
| P + W, graph features | 748 | 0 | +22.4% | +14.6% | +23.6% | +14.4% | 0.364 / 0.369 | 88.8% | 86.3% |
| P + L | 0 | 1,222 | +15.9% | +9.6% | +20.1% | +11.1% | 0.278 / 0.311 | 88.6% | 86.3% |
| P + L + W, road-gate thresholds (third version) | 748 | 1,222 | +15.3% | +9.6% | +18.9% | +10.4% | 0.271 / 0.294 | 88.5% | 86.3% |
| P + L + W, route-gate thresholds | 3,300 | 1,413 | +12.9% | +8.2% | +16.2% | +9.4% | 0.240 / 0.259 | 88.9% | 86.2% |
| **full: P + L + W (route gate) + G** | 3,300 | 1,413 | **+12.9%** | **+8.2%** | **+16.2%** | **+9.4%** | **0.240 / 0.259** | **89.3%** | **87.4%** |
| full without L | 3,300 | 0 | +18.7% | +12.8% | +20.3% | +12.9% | 0.322 / 0.330 | 89.8% | 87.5% |
| full without W | 0 | 1,413 | +15.7% | +9.6% | +20.0% | +11.1% | 0.276 / 0.309 | 88.8% | 87.4% |
| full + CNN decisions (bridge to culvert, flyovers) | 3,300 | 1,413 | +14.4% | +9.0% | +17.6% | +10.4% | 0.258 / 0.278 | 89.3% | 87.4% |
| U-Net features: P + L | 0 | 1,269 | +15.8% | +9.5% | +19.6% | +11.2% | 0.275 / 0.303 | 88.6% | 86.3% |
| U-Net features: P + W | 997 | 0 | +21.9% | +13.4% | +23.2% | +14.2% | 0.354 / 0.362 | 89.1% | 86.3% |
| U-Net features: full, the thresholds of the corrector without U-Net (0.3 / 0.5) | 3,887 | 1,396 | +12.6% | +8.3% | +15.5% | +9.0% | 0.234 / 0.247 | 89.6% | 87.8% |
| **U-Net features: full, as gated (0.3 / 0.4)** | 6,651 | 1,396 | **+11.1%** | **+7.8%** | **+13.8%** | **+8.3%** | **0.215 / 0.230** | **89.9%** | **87.8%** |
| U-Net features: full + bridge and flyover decisions | 6,650 | 1,396 | +12.1% | +8.3% | +14.7% | +8.8% | 0.230 / 0.245 | 89.8% | 87.9% |
| **U-Net alone: its own network, no reader, no corrector** | - | - | **+10.0%** | **+3.0%** | +14.9% | **+5.8%** | **0.160** / 0.218 | 89.4% | 87.6% |

What each step brings, on the town-pair time gap:

| step | effect | why |
| --- | --- | --- |
| pricing (P) | 0 | it changes which links a route trusts, not how long the network says a route is |
| removals | 0 on routes, +0.2 points at 25 m | they cut lines that are not roads, which routes rarely use; off by default, since they can cut a village off |
| white roads to rank 4, old features | 0 | only 101 roads pass the gate |
| graph features for white roads | -1.3 points | 748 roads pass, chosen by betweenness and continuity |
| missing links (L) | **-7.8 points** alone; removing them from the full corrector costs +5.8 | roads cut by expressway and railway bands force detours |
| route-gate thresholds | -2.4 points (town), -2.7 (random) | more links (1,413) and many more white roads (3,300) |
| displacement (G) | 0 on routes; network within 25 m +1.2 points, route km within 25 m +0.4 | only the drawing moves |
| crop-network decisions | +1.5 points (worse) | too few true flyovers, and bridge to culvert is barely better than the base rate; the gate is right to keep them off |
| U-Net features | -1.8 points (town), -2.4 (random) | better link and white-road models, and the route gate then accepts more white roads |
| U-Net alone instead of reader + corrector | -2.9 points (town), -1.3 (random); length gap 8.2% -> 3.0% | the network learned BD TOPO's roads directly, including the stretches the map cuts; it has no pricing and no map checks |

### 2.2 Exercise 1 (the exercise PDF)

All runs pass the 19 checks.

| variant | IT-1 | IT-2 | IT-3 | total km | total time |
| --- | --- | --- | --- | --- | --- |
| image only | 62.6 km, 80 min | 60.7 km, 78 min | 81.6 km, 103 min | 205 | 260 min |
| P | same | same | same | 205 | 260 min |
| P + W | 62.6 km, 79 min | 60.7 km, 76 min | 80.0 km, 101 min | 203 | 256 min |
| P + L | 78.3 km, 103 min | 59.3 km, 73 min | 56.2 km, 68 min | 194 | 244 min |
| P + L + W, third version | 78.3 km, 102 min | 59.3 km, 72 min | 56.2 km, 68 min | 194 | 243 min |
| full without W | 79.4 km, 106 min | 59.9 km, 74 min | 56.2 km, 68 min | 196 | 248 min |
| full without L | 62.6 km, 78 min | 60.7 km, 74 min | 81.3 km, 102 min | 205 | 255 min |
| **full** | **59.9 km, 73 min** | **59.7 km, 74 min** | **70.8 km, 92 min** | **190** | **238 min** |
| U-Net features: full, as gated | 79.4 km, 103 min | 59.9 km, 72 min | 56.2 km, 68 min | 196 | 243 min |
| **U-Net alone** | **58.3 km, 72 min** | 60.3 km, 75 min | 64.3 km, 90 min | **183** | **237 min** |
| BD TOPO | 60.8 km, 73 min | 55.0 km, 66 min | 65.6 km, 79 min | 181 | 218 min |
| OSM | 61.6 km, 74 min | 57.5 km, 71 min | 62.4 km, 76 min | 182 | 221 min |

- **The full corrector is the closest to BD TOPO:** 190 km and 238 min, against 181 km and 218 min.
- **It now finds a western itinerary like BD TOPO's IT-1:** 59.9 km and 73 min, against 60.8 km and 73 min. The third version took a 78 km corridor further west.
- **The U-Net alone is the closest in length** (183 km against 181), and the closest in time (237 min against 218). All three of its itineraries pass the 19 checks, and 91-95% of each lies within 25 m of BD TOPO. With the U-Net features, the corrector goes back to the 78 km western corridor of the third version.
- **Moved lines lie closer to BD TOPO.** With the full corrector, 90-94% of each itinerary lies within 25 m of a BD TOPO road.

### 2.3 The Scenario 1 order (three convoys, four incidents)

The MSR times of the chosen COA:

| variant | C1 LION | C2 TIGER | C3 BEAR | total | against BD TOPO |
| --- | --- | --- | --- | --- | --- |
| P | 124.5 km, 154.8 min; ASR WOLF | 93.9 km, 117.0 min | 93.2 km, 96.1 min; no ASR | 368 min | +13.7% |
| P + W | 124.5 km, 154.1 min | 90.5 km, 115.7 min | 88.7 km, 93.5 min; ASR HAWK | 363 min | +12.3% |
| P + L | 121.5 km, 149.1 min | 89.8 km, 108.6 min | 82.8 km, 85.1 min; ASR HAWK, FOX | 343 min | +6.0% |
| P + L + W, third version | 121.5 km, 148.4 min | 86.3 km, 107.4 min | 82.8 km, 85.1 min; ASR HAWK, FOX | 341 min | +5.4% |
| full without L | 124.8 km, 152.5 min | 90.7 km, 112.1 min | 88.7 km, 90.6 min | 355 min (5 doctrine shortfalls) | +9.8% |
| full without W | 121.5 km, 149.1 min | 89.8 km, 108.6 min | 82.8 km, 85.1 min | 343 min | +6.0% |
| **full** | **121.5 km, 148.0 min** | **86.3 km, 105.2 min** | **81.4 km, 84.2 min; ASR HAWK** | **337 min** | **+4.4%** |
| **U-Net features: full, as gated** | **121.0 km, 147.4 min** | **86.3 km, 104.4 min** | **81.4 km, 83.5 min; ASR HAWK** | **335 min** | **+3.7%** |
| U-Net alone | 118.3 km, 145.7 min | 89.0 km, 108.6 min; no ASR | 80.1 km, 89.1 min; ASR EAGLE | 343 min (1 doctrine shortfall) | +6.2% |
| BD TOPO | 117.2 km, 140.7 min | 85.5 km, 102.6 min | 80.1 km, 80.1 min; ASR HAWK | 323 min | |

- **The full corrector brings the MSRs within 4.4% of BD TOPO's time.**
- **The plan stays valid.** It has three COAs (I=P=E, R, F), and C3's ASR is HAWK, as on BD TOPO.
- **Fewer map checks.** The list falls to 128 stretches to check before the move, from 173 with the third version. With 3,300 white roads at rank 4, routes use more roads the corrector is sure of.
- **Without the links the plan gets worse.** It loses 18 min on the MSRs and shows 5 doctrine shortfalls.
- **The U-Net features give the closest MSRs:** 3.7% from BD TOPO's time.
- **The U-Net alone has the shortest MSRs but is slower.** Its C1 route is the shortest, but the plan has 1 doctrine shortfall and C2 has no ASR. It has no road probabilities, so it cannot price doubtful roads or list them in map checks.
  - It reads 473 bridges, which the reader does not.

### 2.4 The 100 seeds

The same 100 seeds and missions as in `docs/real_bench_results.md`. COA-Net is paired over the seeds; every network planned all 100.

| measure | BD TOPO | image, pricing only | P + L + W, third version | **full (fourth version)** | full + U-Net features | U-Net alone |
| --- | --- | --- | --- | --- | --- | --- |
| valid plans | 84% | 84% | 90% | **90%** | 90% | 91% |
| violations per plan | 0.18 | 0.19 | 0.11 | **0.11** | 0.11 | 0.10 |
| groups with an ASR | 100% | 66% | 82% | **84%** | 84% | 34% |
| on time (simulated) | 90.2% | 83.6% | 88.6% | **89.3%** | 90.0% | 89.7% |
| weighted delay (simulated) | 84.7 | 112.0 | 78.2 | 78.5 | 79.2 | 73.1 |
| MSR time / BD TOPO, median | - | 1.122 | 1.046 | **1.037** | 1.031 | 1.037 |
| MSR length / BD TOPO, median | - | 1.071 | 1.022 | **1.015** | 1.014 | 0.997 |
| MSR length within 25 m / 50 m of BD TOPO | - | 88.9% / 98.5% | 89.1% / 98.7% | **89.9% / 98.9%** | 90.0% / 98.9% | 89.9% / 98.7% |
| design time | 6.5 s | 0.42 s | 0.42 s | 0.38 s | 0.38 s | 0.52 s |
| baselines B0 / B1 valid | 0% / 0% | 18% / 15% | 9% / 5% | 6% / 5% | 5% / 5% | 32% / 30% |

- **The MSRs are within 3.7% of BD TOPO's time and 1.5% of its length**, against 4.6% and 2.2% with the third version.
- **The simulated on-time probability is 89.3%**, against 90.2% on BD TOPO.
- **Validity stays at 90%.** The route gate's extra links and rank-4 roads help at the margin; most of the gain over pricing alone came with the links of the third version.
- **The displacement shows here as well:** MSRs now lie 89.9% within 25 m of BD TOPO.
- **The baselines lose a little more validity** (B0 6%, B1 5%). As before, more alternative routes expose the single-route baselines to more doctrine shortfalls, while COA-Net uses the alternatives.
- **Single points of failure are still invisible** on the image network (0% against 17% on BD TOPO). It does not read bridges over roads and railways.
- **With the U-Net features, the simulated on-time probability reaches 90.0%,** the same as BD TOPO's 90.2%. The MSRs come to 3.1% of BD TOPO's time.
- **The U-Net alone plans as well on validity and time, but finds far fewer alternative routes.** Only 34% of the groups get an ASR, against 84%. Its network is cut wherever the mask breaks, and nothing joins the pieces. The reader's gap joins and the corrector's links are what create the second ways.
  - Its 473 bridges are not enough to show single points of failure either.
  - The single-route baselines do better on it (B0 32% valid), because it gives fewer alternatives that bring doctrine shortfalls.

## 3. What to keep

- **On by default:** pricing, missing links and white roads to rank 4 at the route-gate thresholds, and the displacement.
- **When PyTorch is available, add the U-Net features** (`coa-net scan-unet`, then `scan-learn --unet`). They improve every task that uses them, and the corrector they give is the best on the planner: MSRs 3.1-3.7% from BD TOPO, and 90.0% on time.
- **Off:** removals, which have no effect on routes and a real risk. The bridge and flyover decisions also stay off, from the crop CNN or the U-Net alike: they fail both gates.
- **What matters most.** Missing links do most of the work, then the graph features for white roads, the route-gate thresholds and the U-Net features. The displacement improves where the lines are drawn, not where the routes go.
- **The U-Net alone is not a replacement yet.**
  - It wins on the route test (town pairs +10.0% against +11.1%, exercise 1 183 km against 196).
  - It loses on the planner: 34% of groups with an ASR against 84%, and a doctrine shortfall on the Scenario 1 order.
  - It gives no road probabilities for pricing or map checks.
  - The best use today is the hybrid: reader + corrector with U-Net features.
  - A bigger, pretrained network, plus a link step on its mask, could change this. That is the next experiment.

## 4. Still open

- **The remaining gap.** Town-pair routes are still 11.1% slower than BD TOPO's with the U-Net features. White roads that are importance 4 are still partly missed: the route gate accepts 51% precision. Some roads are not read at all: faint roads and interchanges.
- **A bigger U-Net.** A ResNet-34 encoder U-Net, pretrained on ImageNet and fine-tuned on the H100, in place of the small CPU U-Net. It keeps the same labels, folds and gates. A small network distilled from it can serve as the CPU fallback.
- **The U-Net alone needs a link step.** Joining facing dead ends of its mask, as the corrector's link model does for the reader, would give it the alternative routes it lacks.
- **Resolution.** The reader, the corrector, the crop CNN and the U-Net all work in pixels of this scan. Resample any other scan to the same size on paper (about 0.06 mm per pixel) before using them, or retrain.
- **Using the shapefiles as labels.** Confirm with the organisers that this is allowed.

## 5. Commands

```bash
pip install -e .[scan,cnn]                                     # torch is optional (crop network)
coa-net scan-learn --data DATA --out out/learn                # road gate, crop network, displacement, route gate (about 9 min)
coa-net scan-learn --data DATA --out out/learn --no-cnn --no-geometry --no-route-gate   # the third version
coa-net exercise --data DATA --pdf DATA/exercise_for_preparation.pdf --corrector out/learn --out out/ex_corr
coa-net scan-unet --data DATA --out out/unet                   # U-Net, out of fold (1 h on 4 CPU cores)
coa-net scan-learn --data DATA --out out/learn_unet --unet out/unet   # the corrector with U-Net features
python ablation.py WORKDIR --learn out/learn --learn-unet out/learn_unet --unet out/unet   # delivery-folder script: section 2.1
python unet_graph.py WORKDIR                                  # delivery-folder script: the U-Net-alone network as a graph cache
```

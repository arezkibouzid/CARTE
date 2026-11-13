# Learned corrector, third version: bring the image network's routes closer to BD TOPO

*10 November 2026, branch `scan-roads`. Scenario 1 data, image network loader v7, corrector from `coa-net scan-learn` applied out of fold, so no block is corrected by a model that saw its labels. Earlier versions: `docs/corrector_route_impact.md` (9 Nov), `docs/corrector_pricing_results.md` (10 Nov, pricing). BD TOPO and OSM are the labels and the yardstick; they never steer a route.*

## 1. Where the image routes lose against BD TOPO

The yardstick is the fastest convoy route between each of the 105 pairs of towns in the order's gazetteer, at 60 km/h at most, on roads of rank 5 or better. It is computed on BD TOPO and on the image network.

- **The gap.** Without the corrector, image routes take 24% more time than BD TOPO's (median 1.25) and are 15% longer.
- **Speed costs less than it seems.** BD TOPO's own routes, re-timed with the image's road classes, are 9.8% slower. That is mostly white roads that BD TOPO ranks importance 4 (60 km/h) but the image ranks 5 (40 km/h). The 110 against 90 km/h split on expressways does not matter to a column capped at 60 km/h.
- **Missing links cost the most.** Along BD TOPO's routes, the image network has 343 distinct stretches with no image road within 50 m, with a median length of 100 m. They are short, but they force long detours. For 700 of the 1,380 occurrences, the image's own path between the two ends is more than 5 times the stretch.
- **What these stretches are, on the map.** Most are roads cut where they pass under or over an expressway or a railway: the band is drawn over the road, so the road ends on both sides. The reader's label-gap rule joins a dead end to the nearest line ahead, which is often the expressway band itself. The rest are roads cut by labels, road-number cartouches and roundabout symbols.

## 2. What changed in the corrector

1. **Missing links (new task `link_real`).**
   - **Candidates.** A dead end facing another dead end within 400 m, both pointing at each other (within 45 degrees). Or a dead end facing a junction of another road within 200 m, never onto an expressway. A candidate is kept only when the network has no path between the two that is shorter than 3 times their distance.
   - **Features**, from the image only:
     - distance and alignment;
     - the classes along the straight line (blue fill and the longest blue run, casing, lettering, water, white);
     - the symbols and lengths of the two roads;
     - the network detour;
     - the betweenness at each end.
   - **Label.** A candidate is real when 80% of its straight line lies within 25 m of BD TOPO or OSM.
   - **Result.** 1,828 candidates, 61% of them real. On unseen blocks the model reaches AUC 0.74. At p ≥ 0.5 it adds 1,222 links (248 km), and 74% of them are real.
   - **How an added link is treated.**
     - Each link is a new road with its probability, priced like a label-gap join: 1 + 3 (1 - p).
     - It joins only its two ends, so a road passing an expressway is not connected to it.
     - It is listed first in the map checks.
2. **White-road importance: new graph features.**
   - Sampled betweenness: the number of fastest convoy routes, between 300 x 300 random junctions and within 15-minute trips, that use the road.
   - The road continued straight through its junctions as one line: its length, its number of roads, its white share, and the coloured roads it meets.
   - The width in metres, not only in pixels.
   - **Result.** The AUC of "white road is importance 4" goes from 0.63 to 0.70. At p ≥ 0.7, 748 white roads (406 km) move to rank 4 with 68% precision, instead of 101 roads (58 km) at 65%. Betweenness and continuity are now the most useful features.
3. **A gate for additions.**
   - A correction that changes a fixed rule's decision must still beat the rule's error rate by 1.5 times.
   - An addition has no rule to beat, so it is switched on when at least 70% of what it adds is real on unseen blocks.
   - Removals stay off, as in the second version.

## 3. Results

### 3.1 Routes against BD TOPO (`scripts/bd_gap.py` in the delivery folder)

Town pairs (105):

| network | time / BD TOPO, median [10-90%] | length / BD TOPO, median | total time / BD TOPO | route km within 25 m / 50 m of BD TOPO | km on added links |
| --- | --- | --- | --- | --- | --- |
| image only | 1.247 [1.11-1.43] | 1.146 | 1.237 | 88.8% / 98.6% | - |
| image + corrector, pricing (10 Nov) | 1.248 [1.12-1.43] | 1.146 | 1.238 | 89.0% / 98.6% | - |
| image + importance features only | 1.229 [1.11-1.43] | 1.146 | 1.224 | 88.8% / 98.6% | - |
| **image + corrector, links and importance** | **1.154 [1.04-1.37]** | **1.096** | **1.153** | 88.5% / 98.5% | 24.6 |

Random pairs of points 15-70 km apart (127):

| network | time / BD TOPO, median [10-90%] | length / BD TOPO, median | total time / BD TOPO | route km within 25 m / 50 m of BD TOPO | km on added links |
| --- | --- | --- | --- | --- | --- |
| image only | 1.234 [1.13-1.46] | 1.149 | 1.250 | 88.2% / 98.6% | - |
| image + corrector, pricing (10 Nov) | 1.232 [1.13-1.46] | 1.149 | 1.250 | 88.3% / 98.6% | - |
| image + importance features only | 1.226 [1.12-1.43] | 1.144 | 1.236 | 88.5% / 98.7% | - |
| **image + corrector, links and importance** | **1.176 [1.08-1.35]** | **1.104** | **1.189** | 88.4% / 98.7% | 29.5 |

- **The time gap to BD TOPO falls by about a third:** from 24% to 15% on town pairs, and from 25% to 19% on random pairs. The length gap falls from 15% to 10%.
- **The links do most of it.** The importance features alone take only 1.3 points off.
- **Routes stay on real roads.** 88.5% of their length is within 25 m of BD TOPO and 98.5% within 50 m, as before. They use about 25 km of added links over the 105 routes.
- **Widening the candidate search helps a little.** At 250 m and 40 degrees there are 964 candidates, 72% real, and the gap is 16.2%. At 400 m and 45 degrees there are 1,828 candidates and the gap is 15.3%, at 74% precision.

### 3.2 Exercise 1 (the exercise PDF)

| network | IT-1 | IT-2 | IT-3 | total km | total time |
| --- | --- | --- | --- | --- | --- |
| image only | 62.6 km, 80 min | 60.7 km, 78 min | 81.6 km, 103 min | 205 | 260 min |
| image + corrector, pricing | same | same | same | 205 | 260 min |
| **image + corrector, links and importance** | 78.3 km, 102 min | 59.3 km, 72 min | 56.2 km, 68 min | **194** | **243 min** |
| BD TOPO | 60.8 km, 73 min | 55.0 km, 66 min | 65.6 km, 79 min | 181 | 218 min |
| OSM | 61.6 km, 74 min | 57.5 km, 71 min | 62.4 km, 76 min | 182 | 221 min |

- **The new set is closer to BD TOPO in total:** 194 km and 243 min against 181 km and 218 min. Every run passes the 19 checks, and 90-92% of each itinerary lies within 25 m of a BD TOPO road.
- **The solver picks a different set of three corridors.** Two of the new itineraries run close to BD TOPO's western and middle ones (figure below). The third moves to a corridor further west. The new set is cheaper in total on the corrected network.

Figures: `docs/figures/scan/exercise1-networks.jpg` (one panel per network) and `exercise1-overlay.jpg` (every network on one map).

### 3.3 The Scenario 1 order (three convoys, four incidents)

| MSR | image + corrector, pricing (10 Nov) | **image + corrector, links and importance** | BD TOPO |
| --- | --- | --- | --- |
| C1 LION | 124.5 km, 154.7 min; ASR WOLF | **121.5 km, 148.4 min**; ASR WOLF | 117.2 km, 140.7 min; ASR WOLF |
| C2 TIGER | 93.9 km, 117.0 min; ASR EAGLE, FALCON | **86.3 km, 107.4 min**; ASR EAGLE, FALCON | 85.5 km, 102.6 min; ASR EAGLE, FALCON |
| C3 BEAR | 93.2 km, 96.1 min; no ASR | **82.8 km, 85.1 min**; ASR HAWK, FOX | 80.1 km, 80.1 min; ASR HAWK |
| total MSR time | 368 min (+13.7% on BD TOPO) | **341 min (+5.4%)** | 323 min |
| arrivals | C1 08:50, C2 07:57, C3 07:51 | C1 08:43, C2 07:47, C3 07:40 | |
| chosen COA | valid, 2,339 checks, 16 unknowns | valid, 2,624 checks, 24 unknowns | valid, 5 shortfalls |
| COAs | 2 | 3 (I=P=E, R, F) | 3 |
| map-check stretches | 158 | 173 | - |

- **The MSRs are now within 5% of BD TOPO's time**, instead of 14%.
- **C3 gets alternative routes**, as on BD TOPO. Before, the image network had no second way for C3.
- **The plan stays valid.** The map checks list the doubtful roads and the added links first.

### 3.4 The 100 seeds

The same 100 seeds and missions as in `docs/real_bench_results.md`. COA-Net is paired over the seeds planned on every network.

| measure | BD TOPO | image only | image + corrector, pricing (10 Nov) | **image + corrector, links and importance** |
| --- | --- | --- | --- | --- |
| valid plans | 84% | 84% | 84% | **90%** |
| violations per plan | 0.18 | 0.19 | 0.19 | **0.11** |
| groups with an ASR | 100% | 66% | 66% | **82%** |
| on time (simulated) | 90.2% | 83.4% | 83.6% | **88.6%** |
| weighted delay (simulated) | 84.7 | 90.7 | 112.0 | **78.2** |
| MSR time / BD TOPO, median | - | 1.122 | 1.122 | **1.046** |
| MSR length / BD TOPO, median | - | 1.071 | 1.071 | **1.022** |
| MSR length within 25 m / 50 m of BD TOPO | - | 88.9% / 98.5% | 88.9% / 98.5% | 89.1% / 98.7% |
| design time | 6.5 s | 0.43 s | 0.42 s | 0.42 s |

- **MSRs are within 5% of BD TOPO's time and 2% of its length.** Before, they were 12% and 7% off.
- **Plans improve.** More plans are valid (90%), more convoys get an ASR (82% instead of 66%), and the simulated on-time probability is close to BD TOPO's (88.6% against 90.2%).
- **The added links are the alternative routes.** The image network had too few of them because roads were cut at every expressway and railway.
- **The baselines lose validity on the corrected network** (B0 9%, B1 5%, against 18% and 15%). With more alternatives, the single-route baselines hit more doctrine shortfalls, while COA-Net uses the alternatives.
- **Still a difference with BD TOPO:** the image network still shows no single points of failure (0% against 17% on BD TOPO). It does not read bridges over roads and railways, so it sees fewer critical elements.

## 4. Still open

- The remaining gap on town pairs (15%, against 5% on the planner's MSRs, which use main roads) is half slower white roads (the importance model still misses most importance 4 roads at 68% precision) and half missing roads the candidates do not reach: roads drawn too faint to be read, and interchanges.
- A wrong link costs little, because it is priced at 1 + 3 (1 - p) and listed first in the map checks. Still, 26% of the added links are not on BD TOPO or OSM. On another sheet, check the links on a route before the move.
- Transfer to another sheet is untested. The new graph features are scale-free. The pixel band of the old features is not.
- The shapefiles are used as labels. Confirm with the organisers that this is allowed.

## 5. Commands

```bash
coa-net scan-learn --data DATA --out out/learn            # five tasks; links and white roads to rank 4 pass the gate
coa-net exercise --data DATA --pdf DATA/exercise_for_preparation.pdf --corrector out/learn --out out/ex_corr
python bd_gap.py WORKDIR none out/learn                    # delivery-folder script: routes against BD TOPO
```

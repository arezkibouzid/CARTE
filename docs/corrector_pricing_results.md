# Learned corrector, second version: price doubtful roads, check them before the move

*10 November 2026, branch `scan-roads`. Scenario 1 data, image network loader v7, corrector from `coa-net scan-learn` applied out of fold. "9 Nov" is the first version (`docs/corrector_route_impact.md`, `docs/real_bench_results.md`). "10 Nov" is this version.*

## 1. What changed

| | 9 Nov | 10 Nov |
| --- | --- | --- |
| roads with p(real) < 0.3, label-gap joins with p < 0.5 | removed (352 links) | kept and priced; removed only with `scan-learn --allow-removal` |
| price of a label-gap join | 1 + (1 - p), 1.14 on average | 1 + 3 (1 - p): 1.3 at p = 0.9, 2.5 at p = 0.5 |
| price of another road | up to 2 when p is low | 1 + 3 (0.7 - p) below p = 0.7, else 1 |
| without the corrector | gap join 1.5 in the exercise only | gap join 1.5 in the exercise and in the planner |
| probability in the planner | no (edited network only) | yes: corridor search weights, flow costs, and the doubtful length of each route in the "preferences" objective |
| map checks | none | `map_checks.md` and `map_checks.geojson` for every MSR and ASR of the chosen COA |

Code: `scanlearn.uncertainty_penalty`, `scanlearn.route_checks` and `plan_checks`, `costing.EdgeEval.doubt`, `ArcEval.doubt_min` and `doubt_m`, `candidates.Route.doubt_m`, `design` objective "preferences".

On the Scenario 1 sheet the corrector now prices 1,654 roads as doubtful (p < 0.7) and 2,307 label-gap joins, moves 101 white roads to rank 4, and removes nothing.

## 2. Route study, rerun (300 random pairs, 105 town pairs)

The study of `docs/corrector_route_impact.md` section 3 was rerun: fastest route on roads of rank 5 or better, scored against BD TOPO and OSM, which never steer the routing.

| variant (300 random pairs) | routes found | routes changed | mean time | detour of changed routes, median (max) | length on real roads (25 m) | km on roads labelled not real | km of label-gap joins | km on doubtful roads or gaps (p < 0.7) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| no corrector | 294 | - | 44.24 min | - | 88.97% | 785 | 166 | 146 |
| 9 Nov: removals and light price | 290 | 135 | 44.25 | -0.0% (+1.0%) | 88.93% | 773 | 171 | 135 |
| **10 Nov: cautious price, no removal** | **294** | 151 | 44.26 | +0.0% (+1.9%) | **89.03%** | 771 | 173 | **117** |
| 10 Nov, plus white roads to rank 4 | 294 | 155 | 44.25 | +0.0% (+1.9%) | 89.04% | 770 | 173 | 117 |

| variant (105 town pairs) | routes found | changed | km on doubtful roads or gaps (p < 0.7) | length on real roads |
| --- | --- | --- | --- | --- |
| no corrector | 105 | - | 30.9 | 89.80% |
| 9 Nov | 105 | 70 | 27.0 | 89.76% |
| **10 Nov** | 105 | 65 | **23.6** | 89.80% |

The results:

- **Every route is found again.** The four routes lost on 9 Nov to removals that cut endpoints off are back.
- **Routes take 20% fewer km on doubtful links** than without the corrector (146 to 117 km). On 9 Nov the reduction was 8%. The town pairs show the same: 31 to 24 km.
- **The share on real roads is the best of all variants**, for the same mean time. The largest detour is +1.9%.
- **Label-gap km rise a little** (166 to 173). Most joins have p near 0.9 and now cost about 1.3, below the fixed 1.5. Joins the model doubts cost more, which is why the km on doubtful links fall.

## 3. The COA pipeline on Scenario 1

| run | 9 Nov, with corrector | 10 Nov, no corrector | 10 Nov, with corrector |
| --- | --- | --- | --- |
| chosen COA | valid, 2,332 checks, 16 unknowns | valid, 2,320 checks, 5 T2 shortfalls, 16 unknowns | valid, 2,339 checks, 16 unknowns |
| C1 MSR LION / ASR | 124.5 km, 154.7 min / WOLF | 124.5 km, 154.8 min / WOLF | 124.5 km, 154.7 min / WOLF |
| C2 MSR TIGER / ASR | 93.9 km, 117.0 min / EAGLE, FALCON | same | same |
| C3 MSR BEAR | 93.2 km, 96.1 min | same | same |
| arrivals | C1 08:50, C2 07:57, C3 07:51 | same | same |
| map-check stretches on the MSRs and ASRs | - | 147 | 158 |
| exercise 1 | IT-1 62.6, IT-2 60.7, IT-3 81.6 km, 19/19 | - | same |

- The routes and times do not change: the order's routes had no doubtful link worth avoiding.
- **The 5 T2 shortfalls without the corrector:**
  - What they are: one stretch of 15.5 km with no junction where a control post could go, shared by every route. Spacing posts are placed only at decision points, and the doctrine asks for at most 15 km between posts.
  - Why they appear: the fixed gap price of 1.5, now also in the planner, moves LION by 0.1 min onto that stretch.
  - Effect: they are T2 (preferred) shortfalls, and the plan stays valid.
  - With the corrector they do not appear.

### The map checks

`map_checks.md` lists, for each MSR and ASR:

- first the stretches to check (doubtful roads with p < 0.7, and bridges read on the map, whose load class is unknown);
- then a count of the label-gap joins the corrector believes real, to check if time allows.

For the chosen COA with the corrector:

| route | stretches | km | check first |
| --- | --- | --- | --- |
| LION (MSR C1) | 30 | 2.5 | doubtful roads at km 3.5 (452 m, p 0.47) and km 14.8 (379 m, p 0.55); bridge at km 79.4 |
| WOLF (ASR C1) | 29 | 2.5 | the same two doubtful roads |
| TIGER (MSR C2) | 28 | 1.8 | doubtful road at km 92.9 (152 m, p 0.64); bridges at km 3.1 and 65.9 |
| EAGLE, FALCON (ASR C2) | 27, 28 | 1.8 | the same doubtful road and bridge near the start |
| BEAR (MSR C3) | 16 | 0.9 | bridges only |

So the reconnaissance is down to about ten points on the whole plan. The other stretches are label gaps with p >= 0.74, each listed with its position in the GeoJSON.

## 4. The 100-seed benchmark, rerun

`coa-net real-bench`, the same 100 seeds and missions as on 9 Nov, COA-Net paired over the seeds planned on every network:

| measure | BD TOPO | image, 9 Nov | image + corrector, 9 Nov | image, 10 Nov | image + corrector, 10 Nov |
| --- | --- | --- | --- | --- | --- |
| valid plans | 84% | 84% | 84% | 84% | 84% |
| road-valid | 96% | 100% | 100% | 100% | 100% |
| SPOF groups | 17% | 0% | 0% | 0% | 0% |
| groups with ASR | 100% | 67% | 67% | 66% | 66% |
| on time (simulated) | 90.2% | 83.5% | 83.2% | 83.4% | 83.6% |
| network km | 301 | 165.5 | 165.5 | 165.9 | 165.0 |
| MSR length within 25 m / 50 m of BD TOPO | - | 88.8% / 98.5% | 88.8% / 98.5% | 88.9% / 98.5% | 88.9% / 98.5% |
| design time | 6.5 s | 0.34 s | 0.33 s | 0.43 s | 0.42 s |
| MSR time / BD TOPO MSR time, median | - | 1.122 | 1.122 | 1.122 | 1.122 |

- No plan changes validity on any seed between the four image runs.
- Of 244 convoy MSRs, 19 change by more than 50 m with the corrector between 9 and 10 Nov, and 25 differ between the 10 Nov runs with and without the corrector. The largest change is 2.1 km shorter or 0.45 min slower.
- The MSRs stay on real roads (88.9% within 25 m of BD TOPO, a hair above 9 Nov). On-time probability is unchanged within the noise of 20 simulation runs.
- Design takes about 0.1 s longer, for the extra cost terms.
- The baselines B0 and B1 are unchanged: 18% and 15-16% valid on the image network, 0% on BD TOPO.

**What the benchmark says:** on missions between towns, which follow main roads, the second version keeps the plan quality of the first and of the network without the corrector. What it adds is safety where the network is unsure: no road is deleted, so no endpoint is cut off, routes avoid the links the model doubts (20% fewer km on them in the route study), and every plan comes with its list of checks.

## 5. Still open

- **Transfer.** The corrector is trained on the Scenario 1 sheet, and its pixel features (symbol width, the 6-pixel band) depend on the resolution. On another sheet at the same scale it should hold. At another scale, the pixel features should first be expressed in metres and the model retrained.
- **Bridges and flyovers.** The features still cannot tell a bridge from a culvert (AUC 0.58). The next step is a small CNN or a vision-model check on crops around each bridge in the map-check list.
- **Control-post spacing.** On corridors longer than 15 km with no junction, a spacing post should be placed partway along the corridor, not only at decision points.

## 6. Commands

```bash
coa-net scan-learn --data DATA --out out/learn            # removals off by default (--allow-removal to apply them)
coa-net plan --scan "DATA/Scenario1 100K.tif" --corrector out/learn --places examples/places_scenario1.json \
    --order examples/order_scenario1.txt --max-rank 5 --recce-max 6 --incident "06:40@..." --out out/plan_corr
                                                          # writes map_checks.md and map_checks.geojson
coa-net real-bench --data DATA --places examples/places_scenario1.json --network scan --corrector out/learn --out out/rb_corr
```

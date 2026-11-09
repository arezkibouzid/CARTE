# Impact of the learned corrector on the routes of the COA pipeline

*9 November 2026. Image network loader v7 (dual carriageways on the median), corrector from `coa-net scan-learn`, with out-of-fold predictions so that no block is corrected by a model that saw its labels.*

## 1. Short answer

On Scenario 1 the corrector **does not change the routes COA-Net gives**:

- **The full planner:** with the four test incidents, it gives the same COAs, MSRs, ASRs and times with or without the corrector.
- **Exercise 1:** it gives the same three itineraries.
- **On 405 other origin-destination pairs:** routes change by a few hundred metres at most.

The reason is that the routes were already on real roads. The corrector removes lines that are not roads, but routes never used them. Its real value is elsewhere: it tells you which parts of a chosen route to check (section 5).

## 2. The COA pipeline (planner and exercise)

| run | without corrector | with corrector |
| --- | --- | --- |
| planner, chosen COA | valid, 2,335 checks, 16 unknowns | valid, 2,332 checks, 16 unknowns |
| C1 MSR LION / ASR | 124.5 km, 154.7 min / WOLF | same |
| C2 MSR TIGER / ASR | 93.9 km, 117.0 min / EAGLE, FALCON | same |
| C3 MSR BEAR | 93.2 km, 96.1 min | same |
| COA E (economical) | valid, same routes | same |
| arrivals | C1 08:50, C2 07:57, C3 07:51 | same |
| exercise 1 | IT-1 62.6 km, IT-2 60.7 km, IT-3 81.6 km, 19/19 | same |

The bridge names differ (B335 against B330) only because the bridges are numbered again once roads are removed.

Note: **the planner uses the corrector only through the network it edits** (removed roads, white roads moved to rank 4). The probability that a road is real prices roads in the exercise solver but not yet in the planner's costs.

## 3. Route study: 300 random pairs and 105 town pairs

- **Pairs:** 300 random pairs of junctions on roads of rank 3 or better, 10 to 60 km apart, plus all 105 pairs of the 15 towns in the order's gazetteer.
- **Routing:** fastest route on roads of rank 5 or better.
- **Scoring:** each route is scored against BD TOPO and OSM. These are the labels; they never steer the routing.
- **Script:** `scripts/route_study.py` in the delivery folder, 32 s.

| variant (random pairs) | routes found | routes changed | mean time | detour of changed routes, median (max) | length on real roads (25 m) | label-gap km used | overlap of changed routes with the old route |
| --- | --- | --- | --- | --- | --- | --- | --- |
| fixed rules (no corrector) | 294 | - | 44.24 min | - | 88.97% | 166 | - |
| pricing only | 294 | 129 | 44.24 | -0.0% (+0.1%) | 88.93% | 173 | 99% |
| removals only | 290 | 7 | 44.26 | +0.3% (+1.0%) | 88.97% | 164 | 97% |
| white roads to rank 4 only | 294 | 9 | 44.24 | -0.2% (-0.1%) | 88.98% | 166 | 89% |
| full corrector | 290 | 135 | 44.25 | -0.0% (+1.0%) | 88.93% | 171 | 99% |

The 105 town pairs behave the same way. With the full corrector, 70 routes change, every route is still found, the time is the same, and changed routes overlap the old ones by 99%.

## 4. Why the routes barely move

1. **Routes do not use phantom roads in the first place.**
   - On the 300 routes without the corrector, 785 of 15,303 route-km lie on roads that fail the 25 m test.
   - 700 km of that is within 50 m of a BD TOPO or OSM road: the map's displacement, not phantom roads.
   - None of it is more than 100 m from a real road.
   - The model agrees: these roads have a median p(real) of 0.92, and only 0.7% fall under the removal threshold of 0.3.
   - The 69 km the corrector removes are lines that routes never took.
2. **Calibrated pricing makes label gaps cheaper, not dearer.**
   - The median label-gap join has p(real) = 0.89. Its price becomes 1 + (1 - p), or 1.14 on average, instead of the fixed 1.5.
   - Routes therefore use slightly more gap joins: 166 to 173 km.
   - That is consistent with 85% of the joins being real, but it is a less cautious setting than before.
3. **Removals can cut a village off.**
   - About 18% of removed roads are real roads.
   - The 352 removals (315 roads and 37 joins) leave 26 small pieces (24 km) disconnected, and those pieces are then dropped.
   - As a result, 4 of the 300 random routes are no longer found, because an endpoint was cut off.
   - On this sheet that does not touch the order or the exercise, but it is the one harmful effect seen.
4. **White roads to rank 4** speeds up 9 routes by 0.2%. Only 101 roads change.

## 5. Recommendations

1. **Do not delete; price.** Turn off `drop_road` and `drop_gap`, or remove a road only when that does not disconnect anything (it is not a graph bridge). Pricing doubtful roads keeps every village reachable and costs nothing in route quality here.
2. **Use a cautious price for label gaps**, for example 1 + 3(1 - p): about 1.3 on average, still above 2 for the joins the model doubts. Then calibrated probabilities make routing more careful, not less.
3. **Pass the probability to the planner** (corridor and edge costs), not only to the exercise solver. Today the planner sees only the edited network.
4. **Use the corrector to plan checks, not routes.** For every MSR and ASR, list the stretches with the lowest p(real), plus the label-gap joins and bridges, as reconnaissance or map-check tasks. This is where it adds value when the routes are already right.
5. **Measure where it would matter:** a sheet without shapefiles, using the models trained on Scenario 1 (transfer), and areas dense with labels, where gaps are frequent.

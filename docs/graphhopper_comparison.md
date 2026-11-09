# NET.DISTINCT with GraphHopper: comparison on exercise 1

**Result.** On exercise 1 (3 itineraries, at least 5 km apart, South zone to North zone, Scenario 1 BD TOPO), GraphHopper 11.1 driven by the same separation loop finds **the same best sets** as the built-in SciPy router, with and without turn costs:

- without turn costs: 230.3 min of priced cost, 213.7 min of driving, 177.7 km, smallest gap 5,095 m, 60 critical turns;
- with turn costs: 263.9 min of priced cost including turns, 224.4 min of driving, 186.4 km, smallest gap 6,205 m, 19 critical turns.

Two independent ways of generating first itineraries (via-node lattice, GraphHopper plateau alternatives) reaching the same optimum is evidence that these sets are robust, not an artefact of one heuristic. **GraphHopper is not faster here**: the SciPy searches take 30 s (junction graph) and 45 s (turn-aware), GraphHopper 39 s and 184 s, plus about 32 s to export the roads and import them.

## Method

The comparison isolates the routing engine: everything else is shared.

| | SciPy (built-in) | GraphHopper (`gh.py`) |
| --- | --- | --- |
| Roads | `R.ok`: roads the column may use | the same roads, exported as one one-way OSM way per directed road (way id = edge index + 1) |
| Cost | travel time x T2/T3 penalty | the same cost, encoded as `maxspeed`; the profile drives at `max_speed` (2 km/h resolution) |
| Turn costs (turn-aware variants) | `TurnRouter`, line graph: `turn_min x (deflection/90)^2`, + `sharp_min` above `sharp_deg`, no U-turn | the same formula as `turn_penalty` statements on GraphHopper's `change_angle` (edge-based routing) |
| Zone to zone | virtual source and sink linked to every zone node | one hub per zone, linked to every zone node by fast, high-priority connector ways (turns onto and off them are free) |
| First itineraries | 20 via-node alternatives within 1.6x the fastest | 20 GraphHopper `alternative_route` paths (weight factor 1.6, share factor 0.6, exploration factor 3; 1.5 with turn costs) |
| 5 km separation | roads within 5 km of earlier itineraries masked | the same band, sent as per-request custom-model areas (`priority 0`); exact distance check on `R` afterwards |
| Selection | cheapest feasible set, cost measured on `R` (turn costs included for the turn-aware variants) | the same, with the same `TurnRouter` pricing the turns |

Paths come back as `osm_way_id` path details, so the checker, the facts and the outputs are identical for both engines.

## Results (exercise 1, 3 runs on 9 November 2026, laptop with 8 logical processors)

The itineraries are identical on the 3 runs. Times: median (min-max) of the NET.DISTINCT search; GraphHopper's export of the roads and import add about 32 s per variant (export 19-21 s, import and start 9-12 s).

| engine | priced cost (min) | cost with turns (min) | driving time (min) | km | smallest gap (m) | critical turns | distinct feasible sets | search time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SciPy, junction graph | 230.3 | 331.8 | 213.7 | 177.7 | 5,095 | 60 | 48 | 29.8 s (27.5-33.6) |
| GraphHopper, same roads and costs | 230.3 | 331.8 | 213.7 | 177.7 | 5,095 | 60 | 41 | 39.0 s (37.7-39.2) |
| **SciPy, turn-aware line graph (the outputs)** | 238.0 | **263.9** | 224.4 | 186.4 | 6,205 | **19** | 48 | **45.4 s (43.4-48.8)** |
| GraphHopper with turn costs | 238.0 | 263.9 | 224.4 | 186.4 | 6,205 | 19 | 34 | 184.4 s (95.7-203.5) |

"Priced cost" is travel time times the T2/T3 penalty; "cost with turns" adds the turn-aware router's junction turn costs, the objective of the turn-aware search. The turn-aware sets are 3 % costlier without turns but 20 % cheaper with them, and have 19 critical turns instead of 60.

GraphHopper: 81 queries and 32.5 s inside GraphHopper without turn costs (0.4 s per query, flexible mode, no contraction hierarchies); 77 queries and 170 s (88-189 s) with turn costs (about 2 s per query: edge-based routing explores directed roads, not junctions). Exact separation checks 6-13 s, exclusion bands 0.4 s.

Timings depend on the load of the machine: an earlier single run (5 November) gave 34.9 s for GraphHopper and 46.7 s for the SciPy junction graph, and one on 6 November gave 75.2 s and 69.8 s for GraphHopper and the turn-aware search. Compare engines within one run, not across runs.

## What it took (pitfalls worth knowing at the event)

- **Direction.** A custom model that never mentions `car_access` lets GraphHopper drive one-way ways backwards. The profile starts with `{"if": "!car_access", "multiply_by": "0"}`.
- **Areas.** Two areas with the same id in one request are rejected with an unhelpful `Unable to process JSON`. Each polygon gets its own id (`excl0`, `excl1`, ...).
- **Alternatives.** With the default exploration factor, `alternative_route` returns 1 path on this 60 km trip; with `max_exploration_factor = 3` it returns 20 in 2.6 s. With turn costs (edge-based), factor 3 takes 52 s per query and over 120 s with exclusion areas; factor 1.5 returns 19 paths in 1.9 s.
- **Turn costs.** They need `turn_costs: {vehicle_types: [motorcar]}` on the profile and the `orientation` encoded value. The `turn_penalty` statements sit in the profile's model, not in the request (a request model is merged into the profile's). GraphHopper stores edge orientations in 12-degree steps, so the deflection is a multiple of 12 and each step gets the SciPy cost of that angle; a junction just above the 75-degree "sharp" threshold may be priced as 72 degrees. Our ways are one-way, so GraphHopper's own U-turn rule never applies to a road and its reverse: deflections of 174 degrees and more are forbidden instead.
- **Bands.** Buffering the raw centreline (thousands of vertices) by 5 km took about 15 s per band and used up the time budget after 6 seeds. The line is now simplified (20 m) before buffering and the band enlarged by both tolerances, so it still covers every road the SciPy solver masks (tested).
- **Hubs.** A hub placed on a road makes GraphHopper snap to the road; hubs are moved at least 20 m from any road, inside the data bounds.
- **Paths.** Java runs in GraphHopper's work folder: the GraphHopper home (`--gh-home`) is resolved to an absolute path, a relative one made the jar unreachable.

## Reproduce

Java 17+ and `graphhopper-web-11.1.jar` (Apache 2.0, Maven Central) in a folder with a `jdk*` subfolder: `COA_GH_HOME`, or `../tools` next to the repository.

```bash
pip install -e ".[real,dev]" osmium
coa-net exercise --data DATA --pdf DATA/exercise_for_preparation.pdf --out outputs/exercise1 --graphhopper --gh-turns
```

`--gh-turns` adds the GraphHopper variant with turn costs. The comparison is printed at the end and stored in `outputs/exercise1/metrics.json` (`facts.router_comparison`). Tests: `pytest tests/test_gh.py` (the server tests are skipped without the jar and JDK).

## Limits

- This is GraphHopper as a path engine on our graph and costs, not GraphHopper's own constraint model. Expressing the constraints in its custom-model language (width, surface, towns as areas) would need the BD TOPO attributes as OSM tags, and its slope values need a digital elevation model; that is a separate comparison.
- Speeds are rounded to 2 km/h and turn angles to 12 degrees by GraphHopper, so its path costs can differ slightly from ours; the selection always uses the costs measured on `R`.
- One exercise and one machine: the timings are indicative, and the "same set" is established by identical totals on every indicator (cost, time, length, gap, turns), not by comparing the edge lists.

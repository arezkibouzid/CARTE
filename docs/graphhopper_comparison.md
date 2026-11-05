# NET.DISTINCT with GraphHopper: comparison on exercise 1

**Result.** On exercise 1 (3 itineraries, at least 5 km apart, South zone to North zone, Scenario 1 BD TOPO), GraphHopper 11.1 driven by the same separation loop finds **the same best set** as the built-in SciPy router: 230.3 min of priced cost, 213.7 min of driving, 177.7 km, smallest gap 5,095 m. Its search takes 34.9 s against 46.7 s, plus 28 s to export the roads and import them into GraphHopper. Two independent ways of generating first itineraries (via-node lattice, GraphHopper plateau alternatives) reaching the same optimum is evidence that this set is robust, not an artefact of one heuristic.

## Method

The comparison isolates the routing engine: everything else is shared.

| | SciPy `Router` (built-in) | GraphHopper (`gh.py`) |
| --- | --- | --- |
| Roads | `R.ok`: roads the column may use | the same roads, exported as one one-way OSM way per directed road (way id = edge index + 1) |
| Cost | travel time x T2/T3 penalty | the same cost, encoded as `maxspeed`; the profile drives at `max_speed` (2 km/h resolution) |
| Zone to zone | virtual source and sink linked to every zone node | one hub per zone, linked to every zone node by fast, high-priority connector ways |
| First itineraries | 20 via-node alternatives within 1.6x the fastest | 20 GraphHopper `alternative_route` paths (weight factor 1.6, share factor 0.6, exploration factor 3) |
| 5 km separation | roads within 5 km of earlier itineraries masked | the same band, sent as per-request custom-model areas (`priority 0`); exact distance check on `R` afterwards |
| Selection | cheapest feasible set, cost measured on `R` | the same |

Paths come back as `osm_way_id` path details, so the checker, the facts and the outputs are identical for both engines.

## Results (exercise 1, 4-core laptop CPU)

| engine | itineraries | priced cost (min) | driving time (min) | km | smallest gap (m) | distinct feasible sets | search time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SciPy, junction graph | 3 | 230.3 | 213.7 | 177.7 | 5,095 | 48 | 46.7 s |
| SciPy, turn-aware line graph (the outputs) | 3 | 238.0 | 224.4 | 186.4 | 6,205 | 48 | 69.7 s |
| **GraphHopper, same roads and costs** | 3 | **230.3** | **213.7** | **177.7** | **5,095** | 41 | **34.9 s** |

GraphHopper: 81 queries, 29.2 s inside GraphHopper (about 0.36 s per query in flexible mode, without contraction hierarchies), 5.2 s for the exact separation checks, 0.4 s for the exclusion bands. Export 18.9 s, import and start 8.9 s.

The turn-aware search stays the one that writes the outputs: it is 3 % costlier but has 19 critical turns instead of 60. GraphHopper can price turns too (`turn_costs`), which is the next comparison to run.

## What it took (pitfalls worth knowing at the event)

- **Direction.** A custom model that never mentions `car_access` lets GraphHopper drive one-way ways backwards. The profile starts with `{"if": "!car_access", "multiply_by": "0"}`.
- **Areas.** Two areas with the same id in one request are rejected with an unhelpful `Unable to process JSON`. Each polygon gets its own id (`excl0`, `excl1`, ...).
- **Alternatives.** With the default exploration factor, `alternative_route` returns 1 path on this 60 km trip; with `max_exploration_factor = 3` it returns 20 in 2.6 s.
- **Bands.** Buffering the raw centreline (thousands of vertices) by 5 km took about 15 s per band and used up the time budget after 6 seeds. The line is now simplified (20 m) before buffering and the band enlarged by both tolerances, so it still covers every road the SciPy solver masks (tested).
- **Hubs.** A hub placed on a road makes GraphHopper snap to the road; hubs are moved at least 20 m from any road, inside the data bounds.

## Reproduce

Java 17+ and `graphhopper-web-11.1.jar` (Apache 2.0, Maven Central) in a folder with a `jdk*` subfolder: `COA_GH_HOME`, or `../tools` next to the repository.

```bash
pip install -e ".[real,dev]" osmium
coa-net exercise --data DATA --pdf DATA/exercise_for_preparation.pdf --out outputs/exercise1 --graphhopper
```

The comparison is printed at the end and stored in `outputs/exercise1/metrics.json` (`facts.router_comparison`). Tests: `pytest tests/test_gh.py` (the server test is skipped without the jar and JDK).

## Limits

- This is GraphHopper as a path engine on our graph and costs, not GraphHopper's own constraint model. Expressing the constraints in its custom-model language (width, surface, towns as areas) would need the BD TOPO attributes as OSM tags, and its slope values need a digital elevation model; that is a separate comparison.
- Speeds are rounded to 2 km/h by GraphHopper, so its path costs can differ slightly from ours; the selection always uses the costs measured on `R`.
- One exercise and one machine: the timings are indicative.

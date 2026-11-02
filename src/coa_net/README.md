# coa_net

## Overview

`coa_net` designs one shared, survivable road network for a set of military logistic convoys. The network has main and alternate supply routes (MSR/ASR), route statuses, traffic control posts (TCPs) and halts. The package then schedules every march unit on that network as a "movement credit", presents two to four courses of action (COAs), and repairs the plan when incident reports arrive.

The package runs as a pipeline of stages, S0 to S10:

- **S0 theatre:** builds the road graph and contracts it into corridors.
- **S1 requirements:** compiles doctrine into typed requirements.
- **S2 order and intent:** parses the order, groups convoys and maps the commander's intent.
- **S3 candidates:** generates candidate routes for each group.
- **S4 criticality:** measures how much each element matters and how likely it is to be lost.
- **S5 design:** chooses the network with CP-SAT.
- **S6 refinement:** names routes, places control posts and sets statuses.
- **S7 schedule:** schedules movement credits with CP-SAT.
- **S8 verification and anticipation:** checks every rule, builds contingency playbooks, ranks reconnaissance tasks and runs a simulator.
- **S9 COAs and briefing:** builds the COA comparison and explanations.
- **S10 incident loop:** handles incident reports.

Language models fill eight roles, L1 to L8. They only turn text into typed records (L1 doctrine, L2 order, L3 intent, L6 incident) or rephrase facts the code has already computed (L4 red team, L5 briefing, L7 FRAGO, L8 explanations). They never output a route or a coordinate. Every role has a deterministic fallback, so the whole package runs without a model.

## Interface

Package exports (`__init__.py`): `COA`, `COANet`, `__version__`. Run it as `python -m coa_net` or `coa-net`.

### Session API (`pipeline.py`)

- `COANet`: the session object.
  - **Configuration:** `H`, `requirements`, `catalogue`, `llm`, `use_cpsat=True`, `time_limit_s=10.0`, `sim_reps=100`, `seed=0`, `explain=True`, `recce=True`, `recce_max_elements=None` (cap on the unknown elements valued per COA, for large maps), `data_notes=[]` (data facts and data-driven policy changes, listed in the report under "Data and assumptions").
  - `COANet.from_graph(G, **kw)` wraps an `nx.DiGraph` in a `CorridorGraph`.
  - `plan(order, intent_order=None, intent_eps=None)` runs S2 to S9 and returns the list of `COA`s, default COA first.
  - `simulate(reps, window)`, `choose(name=None)`, `incident(report, time_min=None)`.
  - The `current` property returns `(design, ref, sched, crit)`.
  - Markdown output: `map`, `march_table_markdown`, `routes_markdown`, `annex_markdown`.
  - `report(out_dir, title)` and `metrics()`.
- `COA`: one course of action, holding `design`, `ref`, `sched`, `report`, `playbooks`, `recce`, `sim` and `explanations`, with the properties `name` and `label`.

### CLI (`cli.py:main`)

| Subcommand | What it does |
| --- | --- |
| `demo` | Runs the synthetic theatre with the sample order and the sample incidents. |
| `plan --order FILE` | Plans from an order file. The graph source is one of `--place` (OSM), `--graphml`, `--bdtopo` (GeoPackage, or the organisers' v2 ROUTE `.shp`) or `--synthetic N`. Optional: `--places`, `--dem`, `--incident "HH:MM@text"` (repeatable), `--max-rank N` (corridor backbone), `--recce-max N`, `--out`. |
| `exercise --data DIR --pdf FILE` | Solves an exercise on the organisers' Scenario files; the statement and the sketch are read from the exercise PDF (`--sketch IMG` and `--exercise TEXT` override them). Writes the itinerary and difficult-area layers, the GeoJSON with turns and radii, maps, the decision text and `metrics.json`. Options: `--out`, `--cache`, `--seeds`, `--no-turns`, `--no-compare`, `--no-osm`, explicit file paths. |
| `audit --data DIR` | Audits a Scenario delivery: files, CRS, encodings, OSM/BD TOPO agreement, scan alignment. |
| `compile-doctrine [FILE]` | Compiles doctrine into draft requirements. Option: `--out`. |
| `bench` | Runs the planning and incident benchmarks. Options: `--seeds`, `--convoys`, `--reps`, `--out`. |
| `fetch-osm --place --out` | Downloads an OSM extract and caches it. |
| `templates` | Prints the requirement template catalogue. |

Flags shared by `demo` and `plan`: `--requirements`, `--llm`, `--reps`, `--no-cpsat`, `--coa`.

### Records (`schemas.py`)

- `LocationRef` identifies a place by node, name or xy.
- `VehicleGroup`, `Convoy` and `MissionSpec` describe the order.
- `IncidentType` and `Incident` describe an incident report. `Incident` carries the Admiralty `reliability`/`credibility` grading.

### S0: theatre

- **`graph.py`**
  - Edge normalisation: `normalize_edge`, `finalize_graph`.
  - Edge attribute policies: `UNKNOWN_BY_DEFAULT`, `FALSE_BY_DEFAULT`, `ZERO_BY_DEFAULT`, `CRITICAL_KINDS`.
  - Speeds and road classes: `ROAD_RANK`, `DEFAULT_SPEED_KMH`, `speed_kmh`, `is_two_lane`.
  - Places: `gazetteer`, `resolve`, `location_xy`.
  - Geometry: `point_distance`, `edges_within`, `nearest_node`, `turn_angle`, `ukey`, `set_edge_attr`.
- **`corridors.py`**
  - `CorridorGraph` is the contracted graph `H`: corridors between decision points, and critical `Element`s.
  - `Arc = (corridor_id, ±1)` is one direction of a corridor.
  - `classify` gives the NATO route classification.
- **`loaders.py`**
  - `load_osm`, `save_graphml_osm`, `load_bdtopo`.
  - Enrichment: `add_slopes_from_dem`, `annotate_landuse_osm`, `add_places`, `add_halt_areas`.
  - Helpers: `parse_number`, `osm_edge_attrs`.
- **`scenarios.py`**
  - The seeded synthetic theatre: `synthetic_theatre`, `Theatre`.
  - Random generators: `random_mission`, `random_incident`.
  - Packaged samples: `sample_text`, `sample_incidents`.

- **`realdata.py`** (organisers' Scenario data; needs the `real` extra)
  - Readers: `read_lines` (CRS inferred from lon/lat bounds when there is no `.prj`), `load_scenario_bdtopo`, `load_scenario_osm`, each returning `(G, LoadReport)`.
  - Attribute mapping: `_bd_attrs` (BD TOPO v2 ROUTE fields), `_osm_attrs` and `osm_segments` (Geofabrik fields; ways cut at shared vertices).
  - Graph: `build_graph`, `simplify_chains`, `keep_largest_scc`; slopes with a precision interval (`slope_lo_pct`, `slope_hi_pct`).
  - Policy: `systemic_unknowns` downgrades `forbid` to `risk` for an attribute missing on at least 90 % of the relevant roads; `roadside_halts` makes junctions of three or more main roads halt candidates (`RH-n`) when the data has no halt area.
- **`scanmap.py`**
  - Built-up areas from the scan tint: `builtup_polygons`.
  - OCR (RapidOCR, ONNX, CPU): `ocr_labels`, `towns_from_labels`, `reread_populations`, `town_areas`, `places_from_labels`.
  - Exercise sketch: `georeference_sketch` (place names matched to the scan, RANSAC affine), `sketch_zones`.
- **`exercise.py`**
  - `parse_exercise` compiles an exercise statement into an `ExerciseSpec` and requirement instances (NET.DISTINCT, SEG.TOWN).
  - `RoadArrays`, `Router` (junction graph), `TurnRouter` (line graph with turn costs), `solve_distinct` (k itineraries, pairwise separation, no intersection).
  - Geometry: `chord_radius`, `turns_on`, `itinerary_facts`, `difficult_areas`, `check`.
- **`scenario.py`**: `run_exercise` (the `exercise` command), `ScenarioPaths`, `ScenarioRun`, `EXERCISE_1`, `decision_text`.
- **`audit.py`**: `audit`, `coverage`, `raster_alignment`, `road_ink`, `scan_agreement`.

### S1: requirements and doctrine

- **`requirements.py`**
  - Catalogue: `TEMPLATES` (28 templates over the levels `SEG`, `RTE`, `NET`, `SCH`, `PRO` and the tiers `T0` to `T3`), `Template`, `Param`.
  - The `Requirement` instance, with its `Scope` and `Source`.
  - Loading and saving: `load_requirements`, `save_requirements`.
  - Selecting active rules: `activate`, which returns an `ActiveSet`.
  - Checks: `validate_instance`, `merge_conflicts`.
  - Tier triggers: `tier_from_trigger`, `find_trigger`, `tier_consistent`.
  - Display: `catalogue_markdown`.
- **`doctrine.py`**
  - `compile_doctrine` (role L1) returns `(draft requirements, CompileReport)`.
  - Helpers: `split_sentences`, `pattern_sentence`.

### S2: order, intent and mission

- `parsing.py`:
  - `parse_order` (L2) and `parse_incident` (L6).
  - `parse_time` and `grading_from_text`.
- `intent.py`: `map_intent` (L3), `keyword_intent`, `IntentResult`.
- `mission.py`: `build_context`, `MissionContext`, `Group`, `MarchUnit`, `priority_weights`, `GROUP_WINDOW_MIN`.
- `vehicles.py`: `VehicleType`, `DEFAULT_CATALOGUE`, `load_catalogue`, `convoy_profile`, `merge_profiles`, `Profile`, `HAZMAT_CARGO`.
- `costing.py`: `GroupCoster` evaluates every edge and corridor arc for one group, and returns `EdgeEval` and `ArcEval` records.

### S3 to S5: candidates, criticality and design

- **`candidates.py`**
  - `generate_candidates` returns a `CandidateSet` of `Route`s.
  - Search primitives: `search` (Dijkstra) and `rcsp` (shortest path under limits such as halts, crossings and range).
  - Route building: `make_route`, `route_violations`, `hard_violations`, `halt_plan`, `allowed_halt_nodes`.
  - Survivability: `articulation_points`, `disjoint_pair`.
- **`criticality.py`**
  - `assess` returns a `Criticality` with its `Scenario`s.
  - Building blocks: `structural_impact`, `table_likelihood`, `element_threat`, `red_team` (L4), `impact_table`.
- **`design.py`**
  - Solving: `solve` (CP-SAT, with `greedy` as fallback), `make_coas`.
  - Records: `Design`, `DesignProblem`.
  - Scoring: `evaluate`, `route_terms`, `stability_count`, `coa_label`.
  - Objective settings: `MENU`, `OBJECTIVE_INFO`, `ETA`, `DEFAULT_EPS`, `ARCHETYPES`, `DEFAULT_ORDER`.
- **`baselines.py`**
  - Comparison pipelines `b0` and `b1` (fastest route, no alternate), registered in `BASELINES`.
  - Helper: `make_design`.

### S6 and S7: refinement and schedule

- **`refine.py`**
  - `refine` returns a `Refinement`, made of `NamedRoute`s and `ControlPost`s.
  - Route helpers: `chainage`, `route_story`, `road_name`.
  - Map output: `geojson`.
  - Constants: `ROUTE_NAMES`, `STATUSES`.
- **`schedule.py`**
  - Scheduling: `schedule` (CP-SAT with a greedy fallback), `make_tasks`, `replan_tasks`.
  - Records: `Schedule`, `Credit`, `Movement`, `Task`, `Occupation`.
  - Road checks: `can_meet`, `can_pull_aside`, `headway_min`, `column_length`.
  - Output: `march_table`, `movement_lines`, `hhmm`.
  - Constants: `TICK`, `JUNCTION_CLEARANCE_MIN`.

### S8: verification and anticipation

- `verify.py`: `verify` returns a `VerificationReport` made of `Finding`s. Also `spof_pairs` and `TOL_MIN`.
- `anticipation.py`:
  - Playbooks: `build_playbooks` returns `Playbook`s with `GroupResponse`s; `playbook_diff`.
  - Reconnaissance: `value_of_information` returns `ReconTask`s, built from `recce_candidates`, `answer_classes` and `answer_priors`.
  - Routing helpers: `next_arc_tree`, `tree_path`.
- `simulate.py`:
  - Running: `monte_carlo`, `run_once`, `plan_from`.
  - Records: `SimParams`, `SimPlan`, `SimIncident`, `SimResult`.
  - Incident scripts: `script_uniform`, `script_weighted`, `script_adversary`.

### S9 and S10: COAs, explanations and incidents

- `coa.py`: `facts_table` returns `COAFacts`. Also `facts_markdown`, `template_comparison` and `briefing` (L5).
- `explain.py`:
  - `explain_plan` returns `Contrast`s.
  - Building blocks: `explain_msr_vs_road`, `explain_msr_vs_next`, `explain_spof`, `explain_holds`.
  - Output: `phrase` (L8) and `explanations_markdown`.
- `belief.py`: `BeliefState`, `Report` and `report_probability`, which turns an Admiralty grade into a probability.
- `incidents.py`: `handle_incident(state, inc)` returns an `IncidentOutcome`. Also `PlanState`, `locate` and `positions`.
- `orders.py`: `write_frago` (L7), `frago_template`, `PlanDiff`, `UnitChange`.

### Output, LLM client and benchmark

- `render.py`:
  - SVG drawings: `render_map`, `render_movement`, `render_coa_chart`. Above `LARGE_MAP_ROADS` (20,000) roads, `render_map` draws only the backbone's road ranks, merged into one path per style, and names only the bridges and tunnels of the designated routes and incidents.
  - HTML: `write_html` and `md_to_html`.
  - Interactive map: `render_folium`, which needs folium and has no caller in the package.
- `llm.py`:
  - Client protocol: `LLMClient`.
  - Clients: `OpenAICompatibleClient`, and `ScriptedLLM` for tests.
  - Setup and checks: `from_env`, `call_validated`, `unsupported`, `Usage`.
- `bench.py`:
  - Benchmarks: `run_planning_bench`, `run_incident_bench`.
  - Reporting: `to_markdown`, `incidents_markdown`, `bootstrap_ci`, `network_metrics`, `PIPELINES`.
- `data/` holds packaged inputs: `seed_requirements.json`, `sample_doctrine.txt`, `sample_order.txt` and `sample_incidents.json`.

## Invariants

### Language-model boundary

- **No model output goes in unchecked.** Every model call goes through `call_validated`, with one retry by default. When no model is configured, or the output fails validation, the caller uses its deterministic fallback. [llm.py:call_validated]
- **Generated text cannot introduce new facts.** Text from a model (FRAGO, briefing, explanation) is kept only if `unsupported(text, facts)` finds no number, time or identifier missing from the facts. Otherwise the template text is used, with `source='template'`. [llm.py:unsupported, orders.py:write_frago, coa.py:briefing, explain.py:phrase]
- **Doctrine and intent quotes must be verbatim.** A compiled requirement needs a verbatim quote from the doctrine and a tier that matches its modal trigger. Compiled requirements are always `status='draft'`, and only `approved` ones are ever activated. [requirements.py:validate_instance, doctrine.py:compile_doctrine, requirements.py:activate]
- **The intent mapper is constrained.** Its model output must be a permutation of `MENU`, with verbatim quotes and tolerances within [0, 0.5]. Otherwise the keyword mapper is used. [intent.py:map_intent]
- **Doctrine compilation does not mix paths.** When a model is configured, `compile_doctrine` uses only model instances. The pattern compiler does not run, even for sentences the model left empty. [doctrine.py:_llm_instances]
- **Parsed order fields are checked against the code's data.**
  - The intent text must be a verbatim copy of the order.
  - Vehicle types must exist in the catalogue.
  - Places must resolve on the graph.
  - When the model path is rejected, the order parser falls back to patterns and records an ambiguity. The incident parser falls back silently. [parsing.py:parse_order, parsing.py:parse_incident]

### Data and graph

- **Missing map data stays unknown.** Missing numeric limits stay `None` (`UNKNOWN_BY_DEFAULT`) and are never given an optimistic default. However:
  - boolean flags default to `False` (`FALSE_BY_DEFAULT`);
  - the OSM loader assumes major roads are paved and uses the posted weight on bridges as the MLC;
  - BD TOPO always sets `mlc=None`.

  [graph.py:normalize_edge, loaders.py:osm_edge_attrs, loaders.py:load_bdtopo]
- **Unknown values are evaluated three ways.** An undecided requirement under `on_unknown='risk'` leaves the edge usable and records it as unknown. Under `forbid` or `recce` it blocks the edge. An unknown tunnel clearance always blocks. Only tiers T0 and T1 can block. [costing.py:GroupCoster.evaluate]
- **Corridors are rebuilt by `ensure_decision_point`.** It renumbers the corridors, so `Arc`s and `Route`s held across the call become invalid. `build_context` calls it, so it must run before planning. [corridors.py:CorridorGraph.ensure_decision_point, mission.py:build_context]
- **Direction-free keys use `ukey`.** Edge keys that ignore direction (overrides, element maps) must be built with `ukey`, which applies to both directions of a road. [graph.py:ukey]
- **Coordinate systems.** Graphs from the loaders use `EPSG:4326`, with (lon, lat). Synthetic graphs use `'local'` metres. [graph.py:is_geographic, graph.py:finalize_graph]
- **Scenario files without a CRS.** A shapefile without `.prj` is read as `EPSG:4326` only if its bounds are lon/lat; otherwise `read_lines` raises. Distances, separations and radii are computed in Lambert-93 (`EPSG:2154`). [realdata.py:read_lines, realdata.py:L93]
- **Slopes are not trusted beyond their precision.** A section is blocked by SEG.GRADE only if its low slope bound (end altitudes moved by their stated precision) exceeds the limit; between the bounds it is undecided and follows `on_unknown`. [realdata.py:build_graph, costing.py:GroupCoster.evaluate]
- **Implausible slopes are unknown.** A section steeper than `MAX_PLAUSIBLE_SLOPE_PCT` (35 %) gets no slope and `slope_suspect=True`. [realdata.build_graph]
- **Convoy terminals sit on roads the column can use.** If a group's origin cannot reach the largest strongly connected set of its usable corridor arcs (or its destination cannot be reached from it), `build_context` moves it to the nearest junction of that set, at most `TERMINAL_MAX_MOVE_M` (5 km) away, names the junction if unnamed, and adds a note. [mission._usable_terminals]
- **Systemic unknowns.** When an attribute is missing on at least 90 % of the roads a requirement looks at (bridge load class in BD TOPO and OSM), `forbid` would close every such road, so the policy becomes `risk` and the change is reported. [realdata.py:systemic_unknowns]
- **Exercise itineraries are checked from the geometry.** Separation, intersection, zone membership, towns and blocked roads are recomputed on the output lines, not taken from the solver. [exercise.py:check]

### Design and schedule

- **Objectives are never summed with weights.** Each level is minimised in turn, then frozen within `eps·|best| + ETA`. `lateness` is always the first level, and `stability` comes last in repair. [design.py:_solve_cpsat]
- **Structural rules of the design.**
  - Exactly one MSR per group, and at most `max_alternates` ASRs (default 2).
  - Detour guard: an ASR's time is at most `rho` times the group's fastest feasible time.
  - Survivability: an element shared by the MSR and every ASR is forbidden under T1, accepted only if it is irreducible or waived, and left unconstrained under T2.

  [design.py:_build]
- **Fallbacks when CP-SAT fails.** If CP-SAT is infeasible, the solver first looks for a minimum set of T1 waivers. If none restores feasibility, `solve` falls back to `greedy`. The greedy design and greedy schedule have status `heuristic` and do not guarantee the T1 survivability, network-size or separation rules. [design.py:solve, design.py:greedy, schedule.py:_greedy]
- **The schedule works in ticks.** It uses integer ticks of `TICK = 0.1` min. It enforces headway at both ends of shared arcs, single-lane meetings (`can_meet`), junction clearance, halt-area capacity, and SCH.WINDOW (hard under T1). [schedule.py:_solve_cpsat]
- **Determinism.** CP-SAT runs with `num_workers=1` and `random_seed=0`, which is deterministic unless a time limit is hit. The simulator uses common random numbers keyed by `(seed, rep, source, …)`. Changing those keys breaks comparability between plans. [design.py:_solve_cpsat, schedule.py:_solve_cpsat, simulate.py:_rng]

### Verification and incidents

- **What makes a plan invalid.** A plan is valid if and only if no finding is a T0/T1 `violation`. Unknown data is reported as `unknown` ("priced as risk"), never as a violation. T2 rules only produce `shortfall` findings. [verify.py:VerificationReport.ok, verify.py:_check_roads]
- **The verifier does not trust the plan.** It re-evaluates every MSR/ASR edge against the current map and beliefs, not against the plan's cached costs. [verify.py:_check_roads]
- **How an incident is handled.** `handle_incident` picks the first mode that applies:
  1. A breakdown gives `reschedule` (re-timing only).
  2. A recce result gives `recce`.
  3. If nothing was hit, `logged`. A logged threat zone still names each designated route that crosses it (on Scenario 1, C1's alternate route).
  4. Exactly one newly closed element with a matching playbook gives `playbook`.
  5. Otherwise, `repair`.

  Repair frees only the affected units and replans the rest of the mission from current positions. A destroyed bridge is the bridge nearest to the reported place or coordinates; the outcome says which one, and flags a report naming another road or several bridges nearly as close (`located_note`). [incidents.py:handle_incident, incidents.py:_replan]
- **Belief model.** Report probabilities come from the Admiralty grading. A road is closed when `P ≥ 0.5` and suspect when `0.2 ≤ P < 0.5`. Unconfirmed reports decay with a 60-minute half-life. [belief.py:BeliefState.status, belief.py:BeliefState.q_at]
- **Route names stay stable across repairs.** A retired name is never reused for a different route. [refine.py:refine]

### Known failure modes

- An unknown vehicle type raises `KeyError` in `convoy_profile`. [vehicles.py:convoy_profile]
- `plan` raises `ValueError` when the order has no convoys. [pipeline.py:COANet.plan]
- `report` requires `plan` to have run first. [pipeline.py:COANet.report]
- The template catalogue must have exactly 28 entries, checked by an assert at import time. [requirements.py:TEMPLATES]

## Side Effects

- **Network calls:**
  - HTTP POST to `{COA_LLM_BASE_URL}/chat/completions` for each model role. [llm.py:OpenAICompatibleClient._post]
  - OSM downloads through osmnx. [loaders.py:load_osm, loaders.py:save_graphml_osm, loaders.py:annotate_landuse_osm]
- **Environment variables:** `COA_LLM_BASE_URL`, `COA_LLM_MODEL`, `COA_LLM_API_KEY`, `COA_LLM_JSON_SCHEMA`. [llm.py:from_env]
- **Disk reads:**
  - Order, doctrine and places files. [cli.py]
  - GraphML, BD TOPO and DEM files. [loaders.py]
  - Scenario shapefiles, scans and the exercise sketch. [realdata.py, scanmap.py, scenario.py]
  - Packaged `data/`. [requirements.py:load_requirements, scenarios.py:sample_text]
  - The vehicle catalogue JSON. [vehicles.py:load_catalogue]
- **Disk writes:**
  - `report.html`, `panel_NN.svg`, `network.geojson`, `facts.md`, `fragos.txt`, `metrics.json`. [pipeline.py:COANet.report, render.py:write_html]
  - The requirements JSON. [requirements.py:save_requirements]
  - The bench markdown. [cli.py:cmd_bench]
  - The GraphML cache. [loaders.py:save_graphml_osm]
  - The folium map. [render.py:render_folium]
  - Exercise outputs: `itineraries.geojson`, `difficult_areas.geojson`, `zones.geojson`, `towns_over_threshold.geojson`, `map_*.png`, `decision_process.txt`, `metrics.json`, and the OCR, built-up and zone cache. [scenario.py:run_exercise, scanmap.py:ocr_labels]
- **Graph and state mutation:**
  - `finalize_graph` and `normalize_edge` rewrite edge attributes.
  - `BeliefState.apply` writes `closed`, `closed_p`, `threat` and `weather` into `G`.
  - `RECCE_RESULT` writes the reported attributes onto edges.
  - `add_articulation` adds elements to the shared `H`.
  - `PlanState` is mutated in place by every incident.

  [graph.py:finalize_graph, belief.py:BeliefState.apply, incidents.py:handle_incident, corridors.py:CorridorGraph.add_articulation]
- **Global state:** the osmnx `settings.useful_tags_*` lists are extended. [loaders.py:load_osm] The road-name part of the gazetteer is cached per graph (weak reference, rebuilt when the graph's size changes). [graph.py:gazetteer]
- **Compute:**
  - Each CP-SAT `Solve` uses up to `time_limit_s`, and one design runs several solves, one per objective level.
  - `explain_plan` re-solves once per group.
  - `value_of_information` re-plans once per answer class per element (at most `max_elements` elements; what-if worlds re-cost only the changed roads, `GroupCoster.derived`). The plan without the answer is re-solved by the same procedure (`rebase=True`, in the plan and after a repair), and an answer that leaves every group's roads as they are is not re-solved, so that solver differences are not counted as value.
  - OCR of a full 1:100 000 scan takes minutes on a CPU; its result is cached. [scanmap.py:ocr_labels]

  [design.py:_solve_cpsat, explain.py:explain_plan, anticipation.py:value_of_information]
- **Randomness:** only local, seeded `random.Random` instances (scenarios, simulator, bootstrap). The global RNG is never touched.
- **Timing:** wall-clock timings are recorded in `seconds` fields, so they vary from run to run.

## Verification

- Run: `pytest` (configured in `pyproject.toml`, `testpaths = ["tests"]`)
- Run: `coa-net demo --out outputs/demo`
- Run (organisers' data, `pip install -e ".[real]"`): `coa-net exercise --data DIR --sketch IMG --out outputs/exercise1`

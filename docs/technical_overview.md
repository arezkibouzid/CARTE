# AMIAD — technical overview of the enhanced pipeline

Foundation for the final presentation: what the system does, how each decision is made and traced, what it costs to
run, and what the experiments measured. Every number below comes from a run listed in
[`experiments/INDEX.md`](../experiments/INDEX.md) (folder name given in brackets) or from an earlier report in `docs/`.

**Scope of the evidence.** One map sheet: Hackathon, IGN 1:100,000, Châlons / Verdun / Bar-le-Duc, about 210 × 99 km.
Its 10 PDF routing exercises, BD TOPO and OSM serve as references. U-Net scores are *out of fold*: each pixel is
predicted by a model that never saw it in training.

---

## 1. System architecture and pipeline

```
 scanned map (GeoTIFF) ──► U-Net (PRC Attention Res-U-Net, fine-tuned) ──► 7-band probability raster
                                                                            (road p, importance 1..5, structure)
                              calibration (isotonic, BD TOPO labels) ──► calibrated road probability
 probability raster ──► tracing at p ≥ 0.5 (skeleton → graph, 32,475 junctions, 73,305 one-way sections, 10,330 km)
                    ──► Network: Lambert-93 lines, legal speeds, built-up share, cost per section
 [optional] OSM roads the image misses ("complement"), or no model at all (route on OSM / BD TOPO)

 order / exercise ──► points, deployment area, zones to avoid, waypoints (fixed / free order), round trip,
                      objective (length / time), convoy speed, convoys
                  ──► cost = base (m or min) × (1 + threshold penalty) × (1 + λ · risk)    [risk only when TIFF-only]
                  ──► Dijkstra (SciPy, per leg), all waypoint orders, distinct round trip, convoy deconfliction
                  ──► route + facts + decision log + segment trace (alternatives, verdicts, limits)
                  ──► checks against BD TOPO / OSM routes (never change the route)
 operational orders (PDF) ──► extraction (Qwen3-VL-8B) ──► rule search (RAG, deterministic statuses)
                          ──► explanations + chat (trace answers first, local LLM under a token budget)
```

| layer | code | runs on |
|---|---|---|
| U-Net training / inference | `scripts/prc_unet.py`, `scripts/prc_slurm.sh` | 1 GPU |
| tracing, network, solver, facts | `src/coa_net/scanunet.py`, `src/coa_net/pdfexercises.py` | CPU, 1 core |
| uncertainty cost | `Network.price_by_confidence` in `src/coa_net/pdfexercises.py` | CPU |
| route explanation | `src/coa_net/route_explain.py` | CPU |
| constrained distinct itineraries | `interface/backend/constrained.py`, `src/coa_net/exercise.py` | CPU, threads |
| API (FastAPI) | `interface/backend/amiad_api.py` | CPU |
| route chat, context budget | `interface/backend/route_chat.py` | CPU (+ optional LLM) |
| orders, rules, explanations | `interface/backend/orders.py`, `extraction/`, `RAG/` | CPU + Node.js (+ optional LLM) |
| interface (React, MapLibre) | `interface/src/` | browser |
| experiments | `experiments/` | Slurm (CPU / 1 GPU) |

The back end loads every network once and keeps it in memory, then serves map tiles, vectors, routes, convoys,
extractions, explanations and chat. `interface/run.sh` starts it; it is ready in about 1 minute.

## 2. Route cost function and probabilistic uncertainty modelling

### 2.1 Cost

For every one-way section *e* the solver minimises the sum of

```
cost(e) = base(e) × (1 + s · share_below_τ(e)) × (1 + λ · risk(e))
```

- **`base(e)`** is the length in metres, or the time in minutes. Time uses the legal limit (80 km/h on roads,
  110 / 130 km/h on expressways), 50 km/h in built-up areas, and is capped by the convoy speed.
- **`share_below_τ(e)`** is the operator threshold penalty (existing, optional). It is the share of the section whose
  calibrated probability is below the slider value τ, with strength *s*.
- **`risk(e)`** is the new probabilistic term:
  - `risk(e) = mean over the 20 m samples of −ln(c)`;
  - `c = clip(p_cal / p_ref, 0.05, 1)` is the calibrated U-Net road probability, normalised by its plateau on certain
    roads (`p_ref` = 0.78; calibrated values cap there because labels are 3 px wide);
  - `exp(−risk)` is the section's *confidence*, the geometric mean of `c`;
  - `risk` is 0 on a road the model is sure of, 0.69 where it is half as sure, and 3.0 at the floor.
- **λ is the strength.** It is the number of extra metres (or minutes) the convoy accepts per metre and per nat of
  doubt. The route therefore trades length for the log-likelihood that the roads it uses exist.

**When it applies.** Only when the route relies exclusively on the TIFF: a U-Net network, not completed by OSM. When the
network is completed by OSM, or the route is computed without a model, the term is switched off and the result says
why. `uncertainty_cost_note` gives the reason in the API, in the decision log and in the interface. OSM sections added
to a network are never judged by the U-Net.

**What the interface shows.**
- Config tab: the cost switch and the λ slider.
- Per leg: the route confidence, km of low confidence, and the share of the cost due to uncertainty.
- Per segment: the uncertainty cost.
- In the alternatives: the route without the term.

### 2.2 Choice of λ — measured, not guessed

Run `20261113-204810_routing_ablation`, on the 10 exercises:
- **Real roads:** the share of the route within 25 m of *any* BD TOPO road, and the km beyond that.
- **Closeness:** the gap and overlap with the BD TOPO route of the same exercise.
- **Image confidence:** measured on the same raster for every route.

| λ | km off real roads (10 routes) | \|length gap\| vs BD TOPO | overlap with BD TOPO route | image confidence | mean length |
|---|---|---|---|---|---|
| 0 | 117.6 | 4.0 % | 0.72 | 0.847 | 152.3 km |
| 0.1 | 117.0 | 3.8 % | 0.72 | 0.848 | 151.9 km |
| 0.25 | 117.6 | 3.8 % | 0.72 | 0.849 | 152.0 km |
| **0.5 (default)** | **107.3 (−9 %)** | 4.6 % | 0.68 | 0.862 | 153.0 km |
| 1 | 106.8 | 5.8 % | 0.52 | 0.876 | 154.8 km |
| 2 | 113.3 | 7.4 % | 0.39 | 0.888 | 157.1 km |
| 4 | 112.5 | 8.5 % | 0.33 | 0.893 | 158.6 km |
| 8 | 122.7 | 9.1 % | 0.27 | 0.895 | 159.3 km |
| threshold τ = 0.3, s = 3 | 116.5 | 4.1 % | 0.72 | 0.852 | 152.2 km |

**λ = 0.5 is the default: the knee of the curve** (`experiments/analysis/lambda_choice.png`).
- **At λ = 0.5:** 9 % fewer km on roads that are not in BD TOPO, for +0.5 % length.
- **At λ = 1:** the same gain, but the route moves much further from the reference route.
- **Beyond 1:** the solver takes long detours on other doubtful roads, and km off real roads rise again.

The operator threshold penalty alone barely changes the routes.

## 3. Conversational agent: explaining route decisions

### 3.1 The solver's trace (no language model)

`coa_net/route_explain.explain_route` turns the solved route into a trace built only from solver data. It takes
0.01–0.19 s per route; the cost of explainability is measured in section 7.

**Segments.** The chosen sections are grouped into at most about 14 readable segments: same road class (U-Net
importance 1–5, or the OSM / BD TOPO class), cut at every waypoint. For each segment the trace records:
- **What it is:** length, time, legal speeds, km in built-up areas, drawn bridges, road names when known, source
  (image / OSM / BD TOPO), and model confidence (mean and minimum).
- **What it cost:** the objective cost, split into the base term and the uncertainty term.
- **The best local alternative:** the cheapest way between the segment's two ends with its sections forbidden
  (Dijkstra on the same priced network), with its extra cost (%, km, min) and its confidence.
- **The verdict:**
  - *obligé*: no other way inside the allowed network;
  - *net*: the alternative costs ≥ 10 % more;
  - *serré*: the alternative is within 10 %;
  - plus a sentence built from those numbers.

**Route-level alternatives the solver actually compared:**
- the other waypoint orders, with their extra cost;
- the route without the uncertainty term, with its confidence and km of low confidence;
- for a round trip, the unconstrained outbound and return legs;
- the BD TOPO and OSM routes, when computed.

**Data limitations.** Points snapped more than 150 m away, km of low confidence, the fact that classes and speeds
are inferred from map symbols (no width, load or names), and OSM sections not judged by the model.

In the interface, Results tab → "Pourquoi ce tracé ?" is a table of segments with verdict badges. Clicking a row
zooms to the segment and shows the details.

### 3.2 The route chat

Route chat endpoint: `POST /api/route/{id}/chat`; in the interface, the "Questions sur l'itinéraire" panel. Questions
are answered in this order, cheapest first:

1. **Trace answers.** Questions about one segment ("segment 3", "L1S2"), every segment, the alternatives, the
   limits / uncertainty, or "why this route" are answered by templates filled from the trace.
   - Instant (0.001 s) and exact.
   - A segment number that does not exist gets an explicit answer.
2. **The local model, on a packed context.** This covers other questions, and rule questions when orders are selected.
   `ContextPacker` builds the context under a token budget (`AMIAD_CHAT_BUDGET_TOKENS`, default 6,000; the model
   serves 32k), in this order:
   - the question;
   - the route trace, with the segment named in the question in full;
   - the **rules of every selected order, merged by rule id** (the doctrine is shared, so the same rule recurs across
     orders), ranked by relevance to the question, with their status *per order*, full text while it fits, ids only
     beyond;
   - each order's fragments most related to the question;
   - the last two turns of the conversation in full, older ones as their first sentence;
   - the map context.

   A block that does not fit is shortened, then left out. The answer reports the budget used and what was shortened
   or omitted.
3. **Checks before an answer is shown.** The answer is rejected, retried once, then replaced by the deterministic
   answer if any of these hold:
   - every cited segment and rule id must come from the context (JSON schema enum);
   - every number must appear in the context;
   - a rule may be cited only if its text is in the context;
   - a rule's fixed status must not be contradicted, including "ne s'applique pas" said of an applicable rule;
   - a yes/no claim must cite a segment or rule, or say that the context does not allow a conclusion.
4. **Deterministic fallback.** It gives what the trace says about the question (segments with drawn bridges,
   built-up stretches, speed limits) and the rules closest to it, with their status per order.

**Measured.** Exercise 7, the question "does crossing bridges on this route impose a constraint?", 7 extracted orders
at once:
- **Context:** 5,286 / 6,000 tokens, nothing omitted.
- **With the model:** an answer in 2.8 s citing DR-0031 / DR-0029 and the segments with bridges.
- **Without the model:** the deterministic answer in 0.01 s lists the 10 segments with drawn bridges, says their class
  is not readable on the map, and gives DR-0031 with its status in each of the 7 orders.

**Model usage.** From `outputs/orders/llm_calls.jsonl`, the orders chat used 4,930 prompt tokens per call on average
(max 5,595); the route chat 3,277 (max 5,378).

**Known limit.** The checks are heuristics. An 8B model can still make a claim that holds no number, id or status,
e.g. "aucune règle ne s'applique à ces ponts" when it should be "à vérifier". This is why the trace answers and the
deterministic fallback are the reference, and the model only rephrases.

## 4. Flexibility and scenario simulation

Nothing below required a new hard-coded rule in the solver: every scenario is data passed to the same cost-and-Dijkstra
core.

- **Data presets.**
  - TIFF only, TIFF + OSM (reference or complement), TIFF without OSM (BD TOPO check), or no model at all.
  - Four U-Net models: coverage, compromise, precision, reference.
- **One route.** Departure, arrival, waypoints in fixed or free order (all orders tried), distinct round trip,
  deployment area, any number of zones to avoid, objective, convoy speed, and the uncertainty cost (λ) and threshold
  penalty.
- **Several convoys** (new, "Plusieurs convois").
  - Each convoy has its own points, waypoints, order, round trip, speed, objective and priority.
  - Convoys are planned in priority order on the same network.
  - Deconfliction options:
    - *none*;
    - *penalise* sections already used by an earlier convoy (and those running within 20 m), ×(1 + k), with k set by
      a slider;
    - *forbid* them (×1000: still routable when no separate road exists, and the sharing is reported).
  - The result shows each convoy's route in its own colour, the shared km per pair of convoys, and the decision log.
  - "Exercices du PDF → convois" turns the PDF's exercises into a convoy set in one click.
- **Distinct itineraries under constraints** (existing): k routes between two zones, a minimum separation, no crossing,
  vehicle width / turning radius, turn costs, and difficult areas.
- **Different areas.** A new sheet is one entry in `interface/backend/sheets.json` (TIFF, optional BD TOPO / OSM / PDF,
  model rasters).
- **Saved scenarios.**
  - Points, zones, convoys and options are saved as JSON in `outputs/scenarios/`.
  - Every save is a new file, never overwritten, and can be reloaded from the Scénario tab.

## 5. Computational efficiency and hardware requirements

### 5.1 Measured footprints

| item | measured | source |
|---|---|---|
| back end, all 4 U-Net networks + OSM complement + BD TOPO / OSM references in memory | 4.6 GB RAM | `/proc` of the running server |
| one U-Net network + its probabilities only | 0.9–1.1 GB RAM | stress test, `rss_mb` |
| first start (networks, calibration, references) | about 1 min | warm-up steps |
| route of 5–150 km on the whole sheet (32k junctions, 73k sections) | 0.13–0.18 s solve, flat with distance (slope −0.01) | `20261113-202358_stress_test` |
| route explanation | 0.01–0.19 s | same |
| free-order waypoints: 0 → 7 (5,040 orders) | 0.17 → 0.34 s | same |
| zones to avoid: 0 → 60 | 0.16 → 0.59 s | same |
| convoys: 1 → 64 (penalise) | 0.46 → 33.3 s, linear (exponent 1.04), about 0.4–0.5 s per convoy | same |
| extraction of the network in a rectangle, 0.25 → 16 Mpx | 0.3 → 4.6 s | same |
| same workload on 1, 2, 4, 8, 16 cores | 5.0, 5.1, 5.0, 5.1, 4.9 s | same, `threads` |
| U-Net inference, whole sheet (208 Mpx), base method | 15 s (`docs/prc_inference.md`), 23 s in `imagery_ablation` (4 fold models); 7.9 GB VRAM peak at batch 16; 142 W peak | `20261113-202429_imagery_ablation` |
| U-Net fine-tuning, 12k steps | 15.5 min per fold on one H100 (13 steps/s); 1.3 GPU-hours for 4 folds + final model | `docs/prc_inference.md` |
| local LLM (optional), Qwen3-VL-8B bf16 | 17 GB weights; served with 50 % of an H100 (16.6 GiB KV cache, 121k tokens); 1.4–2.8 s per chat answer | vLLM log, `llm_calls.jsonl` |
| disk: sheet TIFF / BD TOPO / OSM | 75 / 43 / 22 MB | `du` |
| disk: one model raster / one checkpoint | 117–174 MB / 59 MB | `du` |
| disk: interface + network caches | 422 + 96 MB | `du` |
| disk: Python environment (with CUDA PyTorch) | 4.2 GB | `du` |

**Routing is single-threaded SciPy Dijkstra on a sparse graph.** More cores do not speed one request up; they serve
concurrent users. Network size sets the cost, not route length.

### 5.2 Energy

Energy measurement method:
- **CPU:** RAPL counters are not readable on the cluster nodes, so CPU energy is *estimated* as CPU seconds × TDP per
  hardware thread (compute nodes: Xeon Platinum 8480CL, 2 × 350 W / 224 threads = 3.1 W per thread; login node: Xeon Gold 6430, 4.2 W). The estimate is labelled
  as such in every run.
- **GPU:** GPU energy integrates `nvidia-smi` power readings every 0.2 s.

| task | energy |
|---|---|
| one route (incl. explanation) | 0.5–1.6 J CPU (estimated) |
| 16 convoys / 64 convoys | 19 J / 104 J CPU (estimated) |
| extraction of 16 Mpx | 14 J CPU (estimated) |
| trace answer in the chat | < 0.1 J (0.001–0.01 s of one core) |
| LLM chat answer | 1.4–2.8 s of a shared H100 (not metered per call; ≤ 700 W TDP, so ≤ 2 kJ) |
| U-Net inference of the whole sheet | about 3.1 kJ GPU (0.9 Wh) |
| U-Net fine-tuning (4 folds + final) | 1.3 GPU-hours (≤ 0.9 kWh at H100 TDP; not metered) |

**Frugality by design.**
- The GPU is needed once per new sheet (inference) and once per new kind of imagery (fine-tuning), not for routing.
- The language model is optional. Every role has a deterministic answer, and the chat answers the common questions
  without it: a trace answer costs about 10⁴ times less energy than a model answer.
- Contexts are capped at 6k tokens.

### 5.3 Minimum and recommended configurations

| deployment | minimum | recommended |
|---|---|---|
| **Routing + interface** (precomputed rasters; CPU only) | 2 x86-64 cores, 4 GB RAM (one model), 3 GB disk (data, caches, Python without CUDA) | 4–8 cores (concurrent users), 8–16 GB RAM (all models + references), SSD 10 GB |
| **+ U-Net inference on new sheets** | 1 CUDA GPU with 8 GB VRAM (7.9 GB measured at batch 16; a smaller batch needs less, not measured) | 1 GPU ≥ 16 GB (e.g. L4 / A10 / A100 / H100): 15–60 s per sheet |
| **+ fine-tuning** | 1 GPU ≥ 24 GB, 64 GB RAM, about 2 h on a mid-range GPU (estimate) | 1–2 H100: 31 min for 4 folds with 2 GPUs (measured) |
| **+ local LLM** (optional) | 1 GPU ≥ 24 GB for Qwen3-VL-8B in bf16 (17 GB weights + KV cache) | 40 GB reserved on an H100 (as deployed: 121k-token KV cache, 4 concurrent requests) |

The browser needs WebGL (MapLibre); the front end is 1 MB of JavaScript.

## 6. Civilian applicability and U-Net transfer learning

### 6.1 Use cases

The pipeline (map image → road graph with confidence → constrained, explained routing) does not depend on the military
purpose. Only the rules (doctrine CSV), the vehicle list and the default criteria do.

- **Emergency management (civil protection, fire and rescue):** routing convoys or heavy vehicles when OSM is
  incomplete or outdated, from scanned or freshly drawn maps. Several convoys with deconfliction match staggered
  evacuations.
- **Humanitarian mapping:** extracting roads from scanned topographic maps or satellite images where OSM is sparse,
  with an uncertainty map that tells volunteers where to check first.
- **Historical and cadastral maps:** the PRC model was itself trained on Swiss Siegfried maps (19th–20th c.), for
  digitising old road networks.
- **Exceptional transport and logistics:** oversize loads (width, turning radius, bridges) with the constrained
  itineraries; the rules CSV can be replaced by road regulations.
- **Network maintenance:** comparing an image-read network with a reference database (BD TOPO / OSM) to flag missing
  or vanished roads (the "mismatch" maps of `docs/prc_mismatch.md`).

### 6.2 How to adapt the U-Net

1. **Labels.** Rasterise a vector road reference (BD TOPO, OSM, a cadastre) onto the new images, as
   `prc_unet.py prep` does. Most of the work is a clean alignment; importance classes are optional (the road head is
   enough for routing).
2. **Fine-tune from the closest weights.** Fine-tune from the PRC weights, or from our IGN-tuned weights for other
   cartographic maps. Keep the encoder and decoder trainable: everything was fine-tuned here; 6–12k steps of 16 tiles
   of 512 px took about 8–15 min on an H100. Use BCE ×3 without Dice (the best loss measured for routing,
   `docs/prc_loss_results.md`). Hold out folds to measure.
3. **Mix sheets.** Adding a second sheet at 50 % of the tiles improved routes (`docs/prc_mix_results.md`). Diversity
   matters more than steps.
4. **Recalibrate.** Fit the isotonic calibration on the new held-out folds (`scripts/calibrate.py`). The uncertainty
   cost and maps use calibrated values, so λ keeps its meaning.
5. **Re-check λ and the tracing threshold.** Run `experiments/routing_ablation.py` on the new area's exercises. It is
   about 20 min on CPU.

**For aerial or satellite images,** the same architecture applies (U-Nets are the standard there), but the domain gap
is larger than between two map styles. Expect to need more labelled tiles, and to train on the target's resolution
(see 6.3: resolution was the most damaging change).

### 6.3 Ablation: fine-tuning and input imagery (measured)

Run `20261113-202429_imagery_ablation`: one H100, 13 min, 103 kJ GPU. Each degraded or restyled version of the sheet
is predicted by the fold models, out of fold over the 4 folds (208 Mpx). Road F1 / AUC at p ≥ 0.5 against BD TOPO:

| imagery | zero-shot PRC (Swiss maps) | fine-tuned, Hackathon only | fine-tuned, Hackathon + 2nd sheet |
|---|---|---|---|
| original | F1 0.09 / AUC 0.53 | **0.59** / 0.960 | 0.58 / **0.967** |
| JPEG quality 20 | 0.11 / 0.53 | 0.59 / 0.954 | 0.58 / 0.963 |
| noise σ = 12 | 0.06 / 0.52 | 0.59 / 0.953 | 0.58 / 0.962 |
| grayscale | 0.02 / 0.50 | 0.48 / 0.868 | 0.46 / 0.876 |
| another palette (channels permuted) | 0.05 / 0.51 | 0.46 / 0.872 | 0.44 / 0.817 |
| half resolution (20 m/px) | 0.02 / 0.51 | 0.34 / 0.836 | 0.34 / 0.829 |
| blur σ = 1.5 px | 0.01 / 0.50 | 0.32 / 0.774 | 0.34 / 0.808 |
| quarter resolution (40 m/px) | 0.00 / 0.50 | 0.23 / 0.779 | 0.30 / 0.801 |
| low contrast (×0.5) | 0.04 / 0.51 | 0.23 / 0.584 | 0.22 / 0.521 |

What this shows for adaptation:
- **Transfer learning is necessary.** Zero-shot on another map style is at chance (AUC 0.53, F1 0.09).
  Fine-tuning on one sheet brings F1 to 0.59.
- **Compression and noise are harmless.** A model works on low-quality scans or compressed tiles without retraining
  (F1 −0.002).
- **The model reads colour and fine line work.** Grayscale or another palette costs 0.11–0.14 F1; lower resolution or
  blur costs 0.24–0.36; low contrast 0.36. A new map style or a new resolution therefore needs fine-tuning on that
  imagery, not just a tolerant model.
- **Diversity helps under shift.** The model trained on two sheets degrades less at quarter resolution (0.30 vs 0.23)
  and with blur (0.34 vs 0.32), at equal F1 on the original. More diverse training data is the first lever for new
  imagery.

The zero-shot figures reproduce those of the earlier report (`docs/prc_unet_and_pdf_exercises.md`: AUC 0.526,
IoU 0.048), measured in an independent run.

## 7. Ablation studies: methodology, configurations, results

**Method.** `experiments/routing_ablation.py` runs every configuration on the same 10 exercises, with the same points,
area, objective and speed. Every route is scored the same way, whatever produced it:
- **Closeness:** gap and overlap (within 25 m, both ways) with the BD TOPO and OSM routes of the exercise.
- **Real roads:** share of the route within 25 m of any BD TOPO road, and km beyond.
- **Image confidence:** geometric mean of the calibrated U-Net probability along the route, measured on the same
  raster for every route.
- **Run figures:** feasibility, snapping, solve time, CPU energy.

Networks are loaded once beforehand, so load time is not counted as routing time.

**Network component** (run `20261113-204810_routing_ablation`):

| configuration | feasible | \|gap\| BD TOPO | overlap BD TOPO | on real roads | km off (10 routes) | solve |
|---|---|---|---|---|---|---|
| U-Net coverage (mix12k_nodice) | 10/10 | **4.0 %** | 0.72 | 90.8 % | 117.6 | 0.40 s |
| U-Net compromise (nodice + TTA) | 10/10 | 5.1 % | **0.75** | 91.8 % | 106.3 | 0.38 s |
| U-Net precision (mix12k + TTA) | 10/10 | 5.7 % | 0.74 | **92.2 %** | **105.8** | 0.24 s |
| U-Net reference (mix12k, with Dice) | 10/10 | 5.1 % | 0.74 | 91.7 % | 112.9 | 0.28 s |
| U-Net coverage + OSM complement | 10/10 | 4.1 % | 0.73 | 91.7 % | 107.6 | 0.74 s |
| no model, OSM | 10/10 | 2.0 % | 0.81 | 98.0 % | 35.4 | 0.68 s |
| no model, BD TOPO (the reference itself) | 10/10 | 0 | 1.00 | 100 % | 0 | 0.77 s |

**Cost component:** see the λ table in section 2.2. The threshold penalty alone barely changes the routes. The
probabilistic cost at λ = 0.5 removes 9 % of the km off real roads. Threshold + λ = 1 behaves like λ = 1.

**Explanation component:** explanation off vs on gives the same routes; +0.08 s per route on average.

**Earlier ablations** (kept under `outputs/hackathon100k/`, reported in `docs/`, indexed in `experiments/INDEX.md`):
- corrector on / off and its components (`docs/corrector_*`);
- PRC zero-shot vs fine-tuned and the training sweep (`docs/prc_sweep_results.md`);
- second training sheet (`docs/prc_mix_results.md`);
- Dice vs BCE (`docs/prc_loss_results.md`);
- clDice and hard sampling (`docs/prc_robust_results.md`);
- TTA and overlap (`docs/prc_inference.md`);
- gap bridging and hysteresis (`docs/prc_retrace_results.md`);
- calibration (`docs/prc_calibration.md`);
- best configuration for coverage vs precision (`docs/prc_best_config.md`).

## 8. Stress tests: configurations, metrics, results

Stress-test run: `20261113-202358_stress_test`. It ran on 16 CPU threads of a DGX node in a Slurm job, with the
network loaded once. The *degraded* part was re-scored with a relative feasibility criterion (below); the run itself
is untouched.

| condition | configuration | result |
|---|---|---|
| **large areas** | 25 random pairs, 3 → 110 km apart, over the whole sheet | 25/25 routed; solve 0.13–0.18 s whatever the distance |
| | extraction of 0.25 → 16 Mpx (the interface's limit) | 0.3 → 4.6 s; BD TOPO completeness 0.90 → 0.73, correctness 0.95 → 0.88 |
| **convoys** | 1, 2, 4, 8, 16, 32, 64 random convoys (15–80 km), penalise | all routed; linear time (exponent 1.04), 33 s for 64; shared km 0 → 3.6 (16) → 42 (32) → 561 (64): the network saturates |
| **deconfliction**, 16 convoys | none / penalise / forbid | shared km 139 / 3.6 / 0.6; total km 876 / 886 (+1 %) / 1,041 (+19 %); slowest convoy 71 / 72 / 89 min |
| **constraints** | free-order waypoints 0 → 7 | all routed; 0.17 → 0.34 s (5,040 orders) |
| | zones to avoid 0 → 60 (2 km squares along the corridor) | all routed; +2.8 % detour; 0.16 → 0.59 s |
| **parameter combinations** | 40 random draws: model (3 U-Nets / none), objective, λ ∈ {0, 1, 4}, speed 50–110, OSM reference / complement, 0 / 3 waypoints, 0 / 5 / 15 zones | 40/40 routed; median 0.28 s; the slowest calls (18–20 s) were the first use of a network (loading), not the solve (≤ 0.58 s) |
| **limited resources** | same workload pinned to 1, 2, 4, 8, 16 cores | 5.0 s on 1 core: no gain from more cores (single-threaded Dijkstra); 5.2 GB peak RAM for that process |
| **incomplete data: random sections removed** (both directions; 10 exercises × 3 draws) | 1 % / 2 % / 5 % / 10 % | feasible 100 % / 100 % / 73 % / 7 %; detour +3 % / +10 % / +33 % / +46 % |
| **incomplete data: unreadable map patches** (1.5 km disks) | 5 % / 10 % / 20 % / 30 % of the sheet | feasible 93 % / 100 % / 87 % / 40 %; detour +5 % / +18 % / +41 % / +25 % |
| **uncertain data: noise on the probabilities** | σ = 0.05 / 0.1 / 0.2 / 0.3 on the calibrated values, λ = 1 | all routed; route overlap with the noise-free route 0.84 / 0.83 / 0.91 / 0.88; length within 0.5 % |

**Feasibility** means a route exists and no point is pushed more than 500 m further from the network than on the
intact network. The run itself used an absolute 1 km criterion. That marked 4 intact exercises (7–10) infeasible, because
one of their points already sits 1.08 km from the traced network. The corrected figures are in
`experiments/analysis/stress_test_feasibility_relative.json`, computed from the saved records, and the script now uses
the relative criterion.

**Reading.**
- **Scalability** is linear in convoys and flat in distance.
- **Robustness:** the pipeline tolerates small gaps and noise, but a road network read from an image is fragile to
  scattered missing sections. 5 % random holes already break a quarter of the routes, which is why the tracing keeps
  p ≥ 0.5 rather than a stricter threshold, and why the OSM complement exists.
- **Concentrated unreadable areas** (stains, folds, labels) are better tolerated than the same share of scattered holes.

## 9. Experimental artifacts, logs and reproducibility

- **Framework.** `experiments/common.py`: every run is a new folder `experiments/runs/<date-time>_<name>_<commit>[_k]`,
  never overwritten. It holds:
  - `config.json`: script, arguments, parameters, input files with size and date;
  - `environment.json`: git commit, branch, dirty flag and diff hash; Python and package versions; host, CPU, RAM, GPUs;
    Slurm job and threads; energy method;
  - `log.txt`, `metrics.json` + `metrics.csv` (records + summary), `resources.json` (wall, CPU, peak RAM, CPU / GPU
    energy per phase, status);
  - figures;
  - `code.diff` when the code was not committed (from now on).
- **Scripts.** `routing_ablation.py`, `stress_test.py`, `imagery_ablation.py` (GPU), and `index.py`, which writes
  `experiments/INDEX.md`. Slurm wrappers: `run_cpu.sh` (16 threads, no GPU) and `run_gpu.sh` (1 GPU; picks the Python
  environment matching the node's CUDA driver). Derived analyses are in `experiments/analysis/`, each naming its
  source run.
- **Reproduce.**
  ```
  sbatch -J routing_ablation experiments/run_cpu.sh experiments/routing_ablation.py [--groups network cost explain]
  sbatch -J stress_test      experiments/run_cpu.sh experiments/stress_test.py [--tests ...] [--quick]
  sbatch -J imagery_ablation experiments/run_gpu.sh experiments/imagery_ablation.py
  .venv/bin/python experiments/index.py
  ```
  Random draws use fixed seeds (`--seed`, default 2026). The inputs are referenced in each `config.json`. Data files
  (TIFF, shapefiles, PDFs) are not in git; the model rasters and checkpoints are under `outputs/hackathon100k/prc/`.
- **Versioning.** The run folders (about 1 MB) are committed with the code that produced them. The runs of 13 Nov 2026
  were made from commit 6ff7618 plus the working-tree changes committed together with them, in the commit that adds
  `experiments/`. Their `environment.json` says "dirty". Failed and quick runs are kept as they are, marked in the
  index.
- **Tests.**
  - `tests/test_route_explain.py`: the uncertainty cost flips a route on a synthetic network, the trace's alternatives
    and verdicts, trace answers, the context budget, rule questions, and scenarios never overwritten.
  - Full suite: 143 passed, 2 skipped.
  - `interface/scripts/ui_smoke.py` drives the interface headless: routes, explanation, chat, OSM complement,
    extraction, 8 convoys and a scenario save, with no JavaScript error.

## 10. Limitations and future work

- **One sheet of evidence.** Every routing figure comes from the Hackathon sheet's 10 exercises. λ, the tracing
  threshold and the calibration should be re-measured on another area (section 6.2).
- **"Off real roads" uses BD TOPO as truth.** BD TOPO can miss tracks, and 25 m tolerates small misalignments. The
  metric is a proxy for "the road exists".
- **Network fragility.** Scattered missing sections break routes quickly (section 8). Next steps:
  - bridging gaps with a learned link model rather than straight lines (straight-line bridging was mostly wrong,
    `docs/prc_retrace_results.md`);
  - routing on a probabilistic graph with candidate links priced by their probability.
- **Uncertainty is per section, not correlated.** Each section's doubt is priced independently. A route through one
  long doubtful corridor and one through many short doubts can cost the same. A chance-constrained or scenario-based
  cost (sampling plausible networks) would treat this better.
- **Convoys are planned sequentially.** The order matters: priority. A joint optimisation (time windows, conflicts at
  junctions rather than shared sections, column lengths) would be the next step. The 64-convoy test shows the network
  saturating.
- **Chat guards are heuristics.** They block unsupported numbers, ids and statuses, but not every wrong qualitative
  claim (section 3.2). The deterministic answers remain the reference.
- **Energy is estimated for CPUs** (no RAPL access). GPU energy is metered for inference only. Fine-tuning and LLM
  energy are bounded by TDP, not measured.
- **Map semantics.** Width, load class, height clearance and road names are not read from the map. The rules that
  depend on them can only be flagged "to check".
- **Civilian adaptation was not run.** Section 6.3 measures sensitivity to imagery changes on IGN maps only. Aerial or
  satellite imagery would need its own labelled data and fine-tuning.

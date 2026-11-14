# Speaker notes — CARTE, 10-minute talk

Generated from `slides.src.html` by `build_deck.py`. Times add up to about 10 minutes.

## 1. Constraint-aware convoy routing that prices its own doubt  (0:15)

CARTE, constraint-aware routing with trust estimation, and carte is also French for map. We plan convoy routes under many constraints at once, and the heart of the work is the routing engine: one cost function and one solver that take every constraint, and that also pay for how unsure we are about each road. Every route comes with its reasons. Reading roads from a scanned map is an optional module that feeds this engine. The image is the application planning eight convoys at once; the three numbers are measured.

## 2. Many constraints, uncertain roads, decisions to justify  (0:20)

Three difficulties. Constraints pile up: deployment area, zones to avoid, waypoints, round trips, speed, vehicles, and several convoys at once. The road network itself is uncertain: databases can be missing or outdated, and a network read from a scan is never certain. And each choice must be justified against the orders. Our answer is one routing core in which hard constraints remove roads and soft constraints price them, the doubt about a road being one of those prices, plus a trace of every decision.

## 3. The whole pipeline: any road network in, one routing core  (0:50)

This is the whole pipeline. On the left, the inputs: the scanned map, vector roads if we have any, the orders, and the operator's scenario. Any of three sources gives the road graph: the optional vision module, which reads the roads from the scan; a vector network such as OSM or BD TOPO; or both, with OSM filling the gaps of the image network. Whatever the source, we get the same graph: each road section with its length, legal speed, built-up share, class, and, when it comes from the image, the model's confidence. The red box is the core of the method, on CPU only. First, hard constraints remove sections. Second, soft constraints price the remaining ones. Third, the search finds the cheapest route, with heuristics for waypoints, round trips and convoys. Out come the routes, a trace of every decision, independent checks against reference routes, and a chat that answers from that trace first.

## 4. Every constraint fits one formula: remove a road, or price it  (0:50)

This one formula covers every constraint we handle. We look for the waypoint order and the route that minimise the sum, over the route's sections, of a base cost times a product of factors one plus w times phi. The base is metres or minutes; minutes use the legal speed, capped by the convoy's speed, and 50 in towns. Hard constraints never get traded: they remove sections from the allowed set A: leaving the area, crossing a zone to avoid, a vehicle that does not fit, and for a round trip, any section within 20 metres of the outbound leg. Soft constraints are traded against distance: each is a feature phi of a section with a weight w: the model's doubt with weight lambda, the operator's threshold, roads already taken by a higher-priority convoy, and turns at junctions. Structure, the waypoint order, round trips and convoy priority, is handled by the search. The practical consequence: adding a constraint means adding an H or a (w, phi) pair, never a new solver.

## 5. The key soft constraint: a price for the model's doubt  (0:30)

The most important soft constraint is the model's doubt. Its probability comes from the U-Net that reads the scan, which I present at the end. Risk is minus the log of the confidence, averaged every 20 metres along the road: zero on a sure road, 0.69 where the model is half as sure, 3 at the floor. With lambda 0.5, one kilometre of a half-sure road costs like 1.35 kilometres of a sure one, so a detour is taken only if it avoids enough doubt. In effect the route trades length against the likelihood that its roads exist. This term is active only when the route depends on the image alone; with OSM filling the network, or with no model, it switches off and the interface says so. The curve is the formula itself, not a measurement.

## 6. Pricing doubt keeps routes on real roads, for almost no extra length  (0:30)

Does it help? We ran the ten exercises of the brief with lambda from 0 to 8, everything else fixed, and checked each route against BD TOPO, which the router never sees. At 0.5, the kilometres driven on roads that are not in BD TOPO fall by 9 percent, for half a percent of extra length. The right chart shows why we stop there: at 1 the gain is the same but routes move away from the reference route, and beyond, the solver takes detours over other doubtful roads and the error rises again. So 0.5 is the knee, chosen by measurement, and it is the default.

## 7. Heuristics that keep every request under a second  (0:25)

Two kinds of engineering keep this fast. On the left, how the graph is built from the probability map: threshold at 0.5, remove small blobs, thin to centre lines, merge junction pixels into nodes, trace the pixel chains into sections, and attach each section's class, bridges, confidence and built-up share. That gives 32 thousand junctions and 73 thousand sections in about 40 seconds of CPU. We tried bridging gaps with straight lines and a hysteresis threshold; neither was trustworthy, so they are off. On the right, the search heuristics. The graph is a sparse matrix with compiled Dijkstra, so a route costs 0.14 seconds on the whole sheet. Free waypoints: instead of routing every order, we fill a small cost matrix and score all orders on it, 5,040 orders in a third of a second. A distinct round trip solves one leg, forbids it, solves the other. Convoys go in priority order. And points snap only to the main connected part of the network.

## 8. Every segment comes with its reason and its rejected alternative  (0:30)

Because every constraint is a term of the cost, the solver can explain itself. The route is cut into readable segments; for each one we rerun Dijkstra with that segment forbidden, so we know the best alternative and its extra cost: 'net' means at least 10 percent more, 'serré' a close call, 'obligé' no other way. On the right, how the chat answers: segment and alternative questions come straight from this trace, in a millisecond. Other questions go to a local 8-billion-parameter model with the orders and their rules packed under a 100,000-token budget: hundreds of orders fit. Answers citing anything absent from the context are rejected and replaced by a deterministic answer. The checks are heuristics, so the trace answers remain the reference.

## 9. NATO doctrine as a checked rule base, matched to each order  (0:35)

The rules come from NATO and US Army doctrine on road movements and bridges: STANAG 2174, STANAG 2015, AEP-3.12.1.5 and AJP-4.4, 214 rules, each kept with its quote, value, scope, conditions and page. The pipeline is deterministic, no language model: the PDFs cut into 3,486 passages, 138 candidates found by patterns, reviewed by the team against the PDF pages. An automatic check finds 196 of the 214 quotes in the source text; the 18 others, mostly from a scanned manual, are flagged in the app. We reran it here and got identical rules and checks. For each extracted order, its requirements are searched with BM25, then each rule's conditions are compared with what the order says: country, bridges, expressways, halts, load class. That gives the status: applicable, to confirm, background, or not applicable; something the order does not say is never treated as false. A number check compares the rule's limit with the order's values. The local model only explains; the status and the sources are fixed by code. On our eight orders, 3 to 36 rules are kept per order.

## 10. One frozen model, four jobs: only the decoding changes  (0:30)

One language model does four jobs, and it is never retrained: it is frozen, loaded once, shared. Reading PDF orders, explaining rules, the chat, and turning a written order into a scenario: same weights, same greedy decoding. The only thing that changes per job is the decoding mask in the middle. At each step, a JSON grammar lets through only the tokens that keep the answer valid. In this illustration, the model wants to write Verdun as the departure, but Verdun is not a known point, so the token is masked and it must choose among the allowed ids: a PDF point, the point P clicked on the map, a waypoint, a coordinate found in the text. So it can never invent a position. One model instead of four, few retries, and the common questions never reach it.

## 11. When orders amend each other, the code decides what is in force  (0:30)

Orders depend on each other: a FRAGO amends an OPORD, an annex completes it. Side by side, the model would see 60, 50 and 40 kilometres per hour as equals. So the code reads the links from the fragments, with their dates, and decides what is in force: 40, because the later FRAGO wins; 150 metres; and for the load, the annex contradicts the OPORD without amending it, so it is a conflict for the command, not something the model settles. In the context window: pick one order and its whole chain comes in; one required block states what is in force; replaced fragments go last and are cut first; and an answer that gives 60 as current is rejected. These are synthetic test orders, since the exercise PDFs are independent.

## 12. Every dependency the app takes into account, and where it stops  (0:30)

Here is every dependency the app takes into account, and honestly where each one stops. New: legs of a mission. The next leg starts where the previous one ended and may not reuse its path; it is reopened only around the shared point if there is no other way out, and the app says so: on a return trip, 1.7 kilometres reused, all around the turning point. Orders amending orders, doctrine rules matched to orders, the map referenced from a written order, convoys deconflicted, constraints in one formula, and the context window. The limits are on the right: explicit references only, quantified items only, sequential convoys, and a 100,000-token budget.

## 13. 400 orders in the window, the right fact out  (0:30)

Can one 8-billion model read a whole archive of orders? We asked for one fact that only one order carries, among up to 400 orders. Left: with the old 6,000-token budget, from 40 orders the order was cut and the fact lost; with 100,000 tokens, found every time, up to 79,000 real tokens, in 6 seconds. Middle: we tried a model of the same size built for a million tokens, Qwen 2.5 7B 1M. It was faster, but missed facts at the longest contexts and never reported a conflict, so we kept Qwen3-VL, which also reads the order pages as images. Right: the cost grows with what is in the window, not with the budget. Chains and conflicts still hold. And when the model misstated a rule's status, the check caught it and the app answered from the rule engine.

## 14. Many convoys on one network, with no hard-coded scenario  (0:25)

Flexibility follows from the formula. Each convoy has its own points, speed, objective and priority, and convoys are planned in priority order on the same network. On 16 random convoys, without deconfliction they share 139 kilometres of road; pricing the roads already taken brings that down to 3.6 kilometres for one percent more distance; forbidding them costs 19 percent. None of this is hard-coded: every option on the right is data passed to the same formula, and scenarios are saved as new files, never overwritten.

## 15. Flat in distance, linear in convoys, fragile only to scattered gaps  (0:30)

Stress tests on the whole sheet. Distance does not matter: 3 to 110 kilometres all solve in under 0.2 seconds. Convoys scale linearly: 64 in 33 seconds. Then degraded data. Unreadable patches, like stains or folds, are tolerated: up to 20 percent of the sheet still routes almost every exercise. Scattered random gaps are the weak point: 5 percent of sections missing breaks a quarter of the routes, and 10 percent almost all. That is why OSM can fill gaps and why learned gap bridging is next. Noise on the confidence changes almost nothing, every constraint stays cheap, and extra cores do not speed up one route, they serve more users. One note: these feasibility figures use a corrected relative criterion, stored as a separate analysis; the run is untouched.

## 16. Ambitious extension: the roads read from the scan itself  (0:40)

Finally, the ambitious part, useful when there is no shapefile at all, no BD TOPO and no OSM: reading the roads from the scan itself. On the left, one window of the map at each step: the scan tile; the U-Net's road probability; the road class it reads; the mask above 0.5; the centre lines; and the road graph, each section with its confidence. On the right, the network: a ResNet-18 encoder, a decoder, and attention gates on the skip links that keep road-like features and damp text and hatching; three outputs: road, class and bridge. It outputs only probabilities: road, five road classes, and bridges. Honestly, it cannot carry every constraint, because a scan holds less information than a road database: no width, load class or bridge class, so those rules stay to check. But unlike a database, it knows how unsure it is, and that doubt is priced in the route.

## 17. Four trained variants, chosen in the app by what you need  (0:30)

The app offers four trained variants of the same network, named as in the Configure tab. On the same window they read almost the same roads; the differences are in minor roads and junctions, and in the whole-sheet figures: coverage finds the most roads and gives the routes closest to the reference; compromise and precision average eight rotated views at inference, 32 seconds instead of 15, for a cleaner and more precise network. Two training ideas did not help routes: more tiles where the model is unsure, and the Dice losses.

## 18. Pretrained on Swiss maps; two IGN images are enough to adapt it  (0:25)

How much data does it take? The network was pretrained on Swiss Siegfried maps; on our IGN scan it reads almost nothing, F1 0.09. Fine-tuned on one sheet, it reads the roads, F1 0.59; with a second sheet the routes get closer to the reference and it holds better under blur or lower resolution. So two images, with labels taken for free from BD TOPO, are enough; the same recipe applies to any civilian scan with a vector map.

## 19. Routing needs only a CPU; the GPU is used once, offline  (0:25)

Frugality in three numbers. Every route runs on one CPU core in a seventh of a second, about one joule, no GPU. A GPU is needed once per new sheet, 15 seconds, and an 8-gigabyte card is enough. Training happens once per new kind of map: 1.3 GPU-hours; the language model is never trained and is optional. To deploy routing: two cores and 4 gigabytes.

## 20. What CARTE brings, and where it stops  (0:20)

To conclude. What the pipeline brings: one formula for every constraint with the model's doubt priced in; every decision explained, with NATO rules and a frozen, checked local model; dependencies kept, between orders, legs and convoys; and a frugal core on one CPU core. The vision extension makes routing possible without any shapefile. Its limits: one sheet of evidence and sequential convoys for the core; for vision, no width, load class or bridge class on a map, and fragility to scattered gaps. Thank you.

**Total: 10:00**

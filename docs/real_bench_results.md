# COA tests on the real maps: BD TOPO against the image network

*9 November 2026, branch `scan-roads`. Scenario 1 data. Image network: loader v7. Corrector: `coa-net scan-learn`, out-of-fold.*

Two tests are reused from the COA-Net work on main:

- the **Scenario 1 order with its four test incidents**;
- the **100-seed benchmark** (`coa-net bench`). It runs on synthetic theatres, so it was ported to the real maps as `coa-net real-bench`, described in section 2.

Each test was run on three road networks: BD TOPO (the pipeline on main), the network read from the 1:100 000 scan, and the same with the learned corrector.

## 1. The Scenario 1 order with four incidents

| | BD TOPO (`--max-rank 4`) | image (`--max-rank 5`) | image + corrector |
| --- | --- | --- | --- |
| COAs | 3, all valid | 2, both valid | 2, both valid (identical) |
| chosen COA | 5,067 checks, 5 shortfalls, 26 unknowns, 1 irreducible | 2,335 checks, no shortfall, 16 unknowns | 2,332 checks, no shortfall, 16 unknowns |
| playbooks / reconnaissance tasks | 28 / 22 | 2 / 0 | 2 / 0 |
| C1 MSR | LION 117.2 km, 140.7 min, ASR WOLF | LION 124.5 km, 154.7 min, ASR WOLF | same |
| C2 MSR | TIGER 85.5 km, 102.6 min, ASR EAGLE, FALCON | TIGER 93.9 km, 117.0 min, ASR EAGLE, FALCON | same |
| C3 MSR | BEAR 80.1 km, 80.1 min, ASR HAWK | BEAR 93.2 km, 96.1 min, no ASR | same |
| arrivals (deadlines C1 10:30, C2 10:00) | C1 08:36, C2 07:43, C3 07:35 | C1 08:50, C2 07:57, C3 07:51 | same |
| bridge on the D766 destroyed (07:30) | hits the plan: playbook PB-11 in 31 s | logged, plan not affected | same |
| run time | 23.6 min | 1.5 min | 1.5 min |

On the image network the MSRs are 6-16% longer and 10-20% slower. The image does not rank white roads, every white road being rank 5 with its lower speed, and it is missing some minor roads.

The image network also sees far fewer critical elements: 645 against 2,755 on BD TOPO. It reads bridges over water only, not over roads or railways, and it reads no tunnels. As a result the image plan has fewer playbooks and reconnaissance tasks. The D766 bridge, for example, is on the BD TOPO plan's network but not on the image plan's.

## 2. 100 random missions (`coa-net real-bench`)

**The missions.** Each seed draws 1 to 4 convoys between two of the order's 15 towns, at least 25 km apart. The vehicle mixes are those of the synthetic benchmark. Departures are between 06:00 and 07:00, with three hours to arrive. The same seed gives the same mission on every network.

**The pipelines.** Every mission is planned by B0, B1 and COA-Net (CP-SAT). Each plan is refined, scheduled, verified and simulated in the same way: 20 Monte Carlo replications with one loss, uniform over the critical elements.

**The cost.** 100 seeds took about 1 hour per image network and 2.5 hours on BD TOPO.

| COA-Net, same 100 seeds | BD TOPO | image | image + corrector |
| --- | --- | --- | --- |
| valid plans (all levels) | 84% | 84% | 84% |
| road-valid (SEG, RTE) | 96% | 100% | 100% |
| groups whose MSR and every ASR share a bridge or tunnel | 17% | 0% | 0% |
| ... of which reducible | 0% | 0% | 0% |
| groups with an ASR | 100% | 67% | 67% |
| on time (simulated) | 90.2% | 83.5% | 83.2% |
| weighted delay (simulated, veh.min) | 84.7 | 110.2 | 113.1 |
| designated network | 301 km | 166 km | 166 km |
| design time | 6.5 s | 0.3 s | 0.3 s |
| MSR time / BD TOPO MSR time, median [10-90%] | 1 | 1.12 [0.80-1.24] (240 convoys) | 1.12 [0.80-1.24] |
| MSR length / BD TOPO MSR length, median | 1 | 1.07 | 1.07 |
| MSR length within 25 m / 50 m of a BD TOPO road | - | 88.8% / 98.5% | 88.8% / 98.5% |

| baselines (valid / reducible SPOF groups) | BD TOPO | image |
| --- | --- | --- |
| B0 | 0% / 100% | 18% / 60% |
| B1 | 0% / 100% | 15% / 70% |
| COA-Net | 84% / 0% | 84% / 0% |

- **COA-Net keeps its advantage over the baselines on both networks.** Its plans are valid as often (84%) and it leaves no reducible single point of failure, while B0 and B1 leave one for 60-100% of the groups.
- **Validity agrees between BD TOPO and the image network on 90% of the seeds.** The image MSRs drive on real roads: 98.5% within 50 m of BD TOPO. They are about 12% slower, for the reasons in section 1.
- **The image network makes plans look safer than they are.** Without bridges over roads and railways, and without tunnels, it finds fewer single points of failure: 0% of groups against 17% on BD TOPO, which is why only 67% of groups get an ASR against 100%. The resilience figures cannot be compared across networks, because each counts losses over its own critical elements. For resilience, the image network must be completed with crossings of roads and railways; it cannot replace BD TOPO there.
- **The corrector changes nothing measurable.** The MSRs are identical on 98% of the seeds, and validity, SPOFs and times are the same. This matches the route study in `corrector_route_impact.md`.

The benchmark was rerun on 10 November with the second version of the corrector (doubtful roads priced, not removed, and the probability passed to the planner): see `docs/corrector_pricing_results.md` section 4.

## 3. Bug found and fixed

Seed 23 crashed COA-Net on every network, BD TOPO included. The cheapest pair of routes sharing no critical element (`candidates.disjoint_pair`) is read from a min-cost flow in which element nodes are shared by every arc that crosses the element. A unit could enter an element at a junction on one arc and leave on the next arc without passing that arc's head, so the arc was not recorded, the route skipped a road, and `refine` failed on a missing edge.

The fix: the decomposition records the arc that owns each edge leaving an element node, and rejects any pair whose arcs do not join. The fix is on main's code path, so it also applies to plans on BD TOPO.

## 4. Commands

```bash
coa-net real-bench --data DATA --places examples/places_scenario1.json --network bdtopo --out out/rb_bd
coa-net real-bench --data DATA --places examples/places_scenario1.json --network scan --out out/rb_scan
coa-net real-bench --data DATA --places examples/places_scenario1.json --network scan --corrector out/learn --out out/rb_corr
```

Each run writes `bench.md` (the table of the synthetic benchmark) and `results.pkl`, with one row per seed and pipeline.

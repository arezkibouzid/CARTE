# Tracing the road network from the U-Net probabilities: hysteresis and gap bridging

*Same out-of-fold probabilities (PRC mix12k, base inference) for every variant; only the tracing changes. Evaluation on the `examples` data only. Hysteresis `low=t`: road pixels are the connected areas of p >= t that reach p >= 0.5 somewhere. Bridging `bridge=R`: each dead end is joined to the road it points at (within R m and 50 degrees) when the probability along the straight link stays >= 0.15 with a mean >= 0.3 and the two ends are not already joined nearby (network distance < 3 x the link).*

## 1. The traced network

| variant | tracing | sections | km | dead ends | separate pieces | largest connected share (nodes) | bridges (km) | tracing time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | p >= 0.5 | 25,098 | 9,952 | 6,685 | 395 | 94.9% | - | 38 s |
| hyst0.3 | low=0.3 | 23,281 | 9,914 | 6,598 | 311 | 96.5% | - | 63 s |
| hyst0.2 | low=0.2 | 23,903 | 9,922 | 7,046 | 240 | 97.3% | - | 66 s |
| bridge150 | bridge=150 | 28,433 | 10,177 | 6,680 -> 4,344 | 144 | 98.3% | 2,176 (224.9 km) | 39 s |
| bridge300 | bridge=300 | 29,013 | 10,353 | 6,680 -> 3,978 | 110 | 98.6% | 2,556 (401.0 km) | 40 s |
| hyst0.3_bridge150 | low=0.3,bridge=150 | 24,932 | 10,017 | 6,594 -> 5,545 | 210 | 97.5% | 991 (103.2 km) | 64 s |

## 2. Routes, exercises and gaps

| variant | network within 25 m of BD TOPO | route score town / random | town / random time gap | pairs reached | 10 exercises: length gap BD TOPO / OSM | overlap BD TOPO / OSM | gap km (BD TOPO roads not followed) | broken-corridor km | road missed km | mismatch zones |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | 88.3% | 0.119 / 0.131 | +7.1% / +5.5% | 90% | 5.1% / 6.2% | 73.7% / 73.2% | 125.3 | 302.0 | 10.1 | 106 |
| hyst0.3 | 87.3% | 0.146 / 0.141 | +9.1% / +6.4% | 91% | 5.6% / 6.7% | 73.5% / 74.2% | 138.0 | 283.9 | 9.2 | 113 |
| hyst0.2 | 86.5% | 0.156 / 0.155 | +10.1% / +7.4% | 91% | 5.3% / 5.9% | 68.6% / 68.8% | 150.0 | 383.4 | 6.2 | 118 |
| bridge150 | 87.7% | 0.116 / 0.132 | +7.0% / +5.3% | 90% | 5.1% / 6.2% | 73.7% / 73.3% | 124.7 | 300.8 | 10.1 | 106 |
| bridge300 | 87.3% | 0.116 / 0.131 | +6.9% / +5.2% | 90% | 5.1% / 6.2% | 73.6% / 73.2% | 124.1 | 302.5 | 10.1 | 105 |
| hyst0.3_bridge150 | 87.0% | 0.144 / 0.140 | +8.9% / +6.4% | 91% | 5.6% / 6.7% | 73.4% / 74.2% | 137.8 | 285.7 | 9.2 | 113 |

### Per exercise: length gap / overlap against BD TOPO

| variant | ex 1 | ex 2 | ex 3 | ex 4 | ex 5 | ex 6 | ex 7 | ex 8 | ex 9 | ex 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | +5.5% / 87% | +5.5% / 87% | +10.3% / 72% | +9.3% / 67% | +4.9% / 82% | +3.1% / 47% | +2.1% / 83% | +1.8% / 80% | +4.4% / 58% | +4.2% / 73% |
| hyst0.3 | +5.8% / 85% | +5.8% / 85% | +11.5% / 69% | +10.8% / 67% | +5.0% / 81% | +3.1% / 47% | +2.4% / 81% | +2.3% / 87% | +4.6% / 53% | +4.7% / 80% |
| hyst0.2 | +6.8% / 84% | +6.8% / 84% | +11.6% / 68% | +11.0% / 66% | -1.6% / 63% | -2.9% / 41% | +1.8% / 80% | +2.0% / 85% | +4.2% / 56% | +4.2% / 60% |
| bridge150 | +5.5% / 87% | +5.5% / 87% | +10.3% / 72% | +9.3% / 67% | +4.9% / 82% | +3.1% / 47% | +2.0% / 83% | +1.8% / 80% | +4.3% / 58% | +4.2% / 73% |
| bridge300 | +5.5% / 87% | +5.5% / 87% | +10.3% / 72% | +9.5% / 67% | +4.9% / 82% | +3.1% / 47% | +2.0% / 83% | +1.8% / 79% | +4.3% / 58% | +4.2% / 73% |
| hyst0.3_bridge150 | +5.8% / 85% | +5.8% / 85% | +11.5% / 69% | +10.7% / 67% | +5.0% / 81% | +3.1% / 47% | +2.3% / 80% | +2.3% / 87% | +4.6% / 52% | +4.7% / 80% |

Maps of each variant's mismatches, with the PRC probability and the gaps: `outputs/hackathon100k/mismatch_retrace_<variant>/`. The traced networks (`network.pkl`, `tracing.json`) are in `outputs/hackathon100k/retrace/<variant>/` and are used as they are by `coa-net pdf-exercises --network prc-alone --unet outputs/hackathon100k/retrace/<variant>`.

## 3. Why bridging cannot close the exercise gaps

The gap points of the base network on the BD TOPO roads the exercise routes do not follow (125.3 km), grouped into runs along the road:

| gap run length | runs | km | runs with p < 0.15 somewhere | runs with a dead end of the PRC network within 60 m |
| --- | --- | --- | --- | --- |
| up to 50 m | 60 | 1.6 | 0% | 13% |
| 50-150 m | 58 | 5.3 | 2% | 36% |
| 150-300 m | 31 | 6.8 | 10% | 26% |
| over 300 m | 85 | 111.6 (89%) | 51% | 28% |

Only 29 runs (2.4 km) are short enough, probable enough and end at a dead end, which is what bridging can join.

## 4. Conclusions

- **Bridging works on the network** (150 m: 2,176 links, 225 km; dead ends 6,680 -> 4,344; separate pieces 395 -> 144; the largest connected part 94.9% -> 98.3% of the nodes) and **helps the routes a little** (route score on town pairs 0.119 -> 0.116; time gap +7.1% -> +6.9% at 300 m), at no extra time (39-41 s per sheet).
- **It does not change the 10 exercises** (length gap to BD TOPO 5.1% / overlap 73.7%, as before): 89% of the gap length on the reference corridors they miss is in stretches over 300 m, half of them where the U-Net sees almost no road (p < 0.15), and most short gaps do not end at a dead end (the PRC network runs nearby on another line). These are roads missing from the probabilities, not breaks of a traced road. The earlier label "road seen, broken in the PRC network" (`prc_mismatch.md`) is therefore too generous: most of those corridors contain long stretches the U-Net does not trace.
- **Hysteresis does not help**: continuing roads down to p 0.3 or 0.2 joins more pieces (91% of the pairs reached) but widens the road areas before thinning, so the centre lines wander (25 m agreement 88.3% -> 86.5-87.3%) and routes get longer (route score 0.146-0.156; exercises +0.2-0.5 point).
- **Recommendation**: use `bridge300` (or `bridge150`) as the default tracing of the PRC network: `coa-net pdf-exercises --network prc-alone --unet outputs/hackathon100k/retrace/bridge300` (or `--tracing bridge=300` with any U-Net folder). The remaining exercise mismatches need the U-Net to see more roads (more labelled sheets, as Scenario 1 showed), not a better tracing.

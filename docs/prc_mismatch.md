# Where the PRC 6k routes differ from BD TOPO and OSM

*Exercise file `exercise_1-to-10-1.pdf`, sheet `Hackathon_100K.tif`. PRC network: `network_from_unet` of the out-of-fold
PRC raster (6,000 steps, the best run of the sweep). Script: `scripts/mismatch_map.py`; outputs (maps, GeoJSON) in
`outputs/hackathon100k/mismatch_prc6k/`.*

## Method

- Each exercise is solved with the same rules on the PRC network, BD TOPO and OSM. The routes are sampled every 10 m; a stretch counts as different when it is more than 25 m from the other route for at least 200 m. Stretches within 1 km of each other form a zone.
- In each zone the PRC road probability (out of fold) is read along the reference road PRC did not follow, and the PRC network is tested for a connected path along that road (edges inside a 40 m corridor joining its two ends).
- Causes: **road missed by PRC** (p < 0.35), **PRC uncertain** (0.35-0.65), **road seen but broken in the PRC network** (p >= 0.65, no connected path along it), **alternative chosen** (seen and connected: PRC had the road and preferred its own way).
- **Gap points**: reference road with no PRC network edge within 25 m (`prc_gaps.geojson`).

## Results

| exercise | route | agrees with both references | off BD TOPO | off OSM | mismatch zones |
| --- | --- | --- | --- | --- | --- |
| 1, 2 | 39 km | 30.4 km (78%) | 6.8 km | 6.8 km | 4 |
| 3, 4 | 103 km | 64-65 km (63%) | 31-34 km | 18-20 km | 7-8 |
| 5 | 195 km | 130 km (67%) | 37 km | 56 km | 13 |
| 6 | 195 km | 76 km (39%) | 100 km | 55 km | 6 |
| 7, 8 | 197-199 km | 101-109 km (53%) | 53-59 km | 81-87 km | 14-16 |
| 9 | 257 km | 98 km (38%) | 108 km | 142 km | 14 |
| 10 | 259 km | 168 km (65%) | 62 km | 65 km | 21 |

Causes over all zones (reference road not followed):

| cause | BD TOPO | OSM |
| --- | --- | --- |
| road seen, broken in the PRC network | 31 zones, 333 km | 31 zones, 346 km |
| PRC uncertain (p 0.35-0.65) | 19 zones, 81 km | 21 zones, 74 km |
| road missed by PRC (p < 0.35) | 18 zones, 11 km | 17 zones, 11 km |
| alternative chosen (road seen and connected) | 27 zones, 25 km | 23 zones, 39 km |

Gap points on the BD TOPO roads not followed: 130 km (OSM: 144 km); 19% in built-up areas; mean PRC probability 0.41,
59% of them below 0.5. By BD TOPO importance: 36% on importance-5 roads (p 0.18), 12% on importance 1 (p 0.35) and 20%
on importance 2 (p 0.61), the rest on importance 3-4 (p 0.54-0.56). BD TOPO itself is 83% importance-5 road by length.

## What it means

- **PRC is not uncertain where it routes**: mean probability 0.97-0.98 along its own routes.
- **The large mismatches are corridors, not local errors** (exercises 6, 7-9): BD TOPO's and OSM's way exists in the PRC probability (p 0.75-0.93) but the network traced from it is broken by short gaps every few kilometres, so a longer but continuous corridor is chosen. One gap is enough to lose a 50 km corridor.
- **Where the gaps are**: minor (white) roads, which PRC hardly sees (p 0.18); expressways and their interchanges (two blue bands, ramps), and junctions and villages, where the probability dips below 0.5 for a few pixels. Text and water are not over-represented at the gaps. Only 19% of the gaps are in built-up areas.
- **Missed roads matter little** (11 km): white roads in forest drawn faintly on the scan.
- **Where the references disagree, PRC often sides with one of them**: in exercise 3 (Camp de Mourmelon) BD TOPO crosses the military camp on a road the scan barely draws; PRC and OSM avoid it.

## What would reduce the mismatches

1. Join the gaps of the traced network: link dead ends to a road within 50-100 m when the probability along the link stays above 0.2 (the corrector's missing-link model does this for the map reader).
2. A lower threshold (0.3-0.4) for continuing an already traced line than for starting one (hysteresis), so short dips at junctions and labels do not cut it.
3. Give the PRC network road classes from its importance output so that expressways and main roads are preferred like on BD TOPO.

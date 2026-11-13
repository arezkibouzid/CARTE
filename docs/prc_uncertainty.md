# Where the PRC U-Net is unsure, where it is wrong, and an operator's probability threshold

*Run mix12k (PRC fine-tuned with Scenario 1), out-of-fold probabilities on the Hackathon sheet; the 10 exercises of
`exercise_1-to-10-1.pdf`. Script: `scripts/uncertainty_map.py`. Outputs: `outputs/hackathon100k/uncertainty_mix12k/`
(`uncertainty_whole_sheet.png`, `uncertainty_cells.tif` for QGIS, `threshold_sweep.json`).*

The operator's option: `coa-net pdf-exercises --network prc-alone --unet DIR --prob-threshold 0.7 [--prob-penalty 3]`.
A road section costs 1 + penalty x (share of the section whose U-Net road probability is below the threshold); lengths and
times reported are never priced.

## 1. Is the uncertainty where the errors are? (cells of 64 x 64 px, about 0.4 x 0.6 km)

| measure | value |
| --- | --- |
| cells with road signal and BD TOPO road | 16,900 |
| Spearman correlation, uncertainty vs share of BD TOPO road missed | 0.31 |
| BD TOPO road missed, all cells | 21.5% |
| BD TOPO road missed, the 20% most uncertain cells | 34.6% (x1.6) |
| exercise gap points (BD TOPO route) in the 20% most uncertain cells | 45% (20% if unrelated) |
| mean uncertainty at the gap points / over the sheet | 0.62 / 0.55 |

Maps: `uncertainty_whole_sheet.png` (top: uncertainty, no labels; bottom: errors, labels; both with the 10 exercises' mismatches and gap points). GeoTIFF for QGIS: `uncertainty_cells.tif` (bands: uncertainty, share of BD TOPO road missed, share of road-signal pixels below 0.7).

## 2. Operator's threshold: sections below it cost 1 + 3 x the share below (mean over the 10 exercises)

| threshold | network km below it | route km below it (mean) | length gap vs BD TOPO | time gap vs BD TOPO | overlap with BD TOPO | length gap vs OSM | overlap with OSM |
| --- | --- | --- | --- | --- | --- | --- | --- |
| no penalty | 0 | 0.0 | 5.1% | 4.9% | 73.7% | 6.2% | 73.2% |
| 0.6 | 107 | 0.0 | 5.1% | 4.9% | 73.7% | 6.3% | 73.2% |
| 0.7 | 307 | 0.2 | 5.6% | 5.1% | 75.0% | 6.7% | 74.3% |
| 0.8 | 502 | 0.4 | 5.8% | 5.3% | 74.5% | 6.9% | 73.7% |
| 0.9 | 811 | 1.8 | 6.6% | 6.1% | 65.8% | 7.7% | 58.8% |

### Per exercise (length gap / overlap against BD TOPO)

| threshold | ex 1 | ex 2 | ex 3 | ex 4 | ex 5 | ex 6 | ex 7 | ex 8 | ex 9 | ex 10 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| no penalty | +5.5% / 87% | +5.5% / 87% | +10.3% / 72% | +9.3% / 67% | +4.9% / 82% | +3.1% / 47% | +2.1% / 83% | +1.8% / 80% | +4.4% / 58% | +4.2% / 73% |
| 0.6 | +5.5% / 87% | +5.5% / 87% | +10.4% / 72% | +9.3% / 67% | +4.9% / 82% | +3.1% / 47% | +2.1% / 83% | +1.8% / 80% | +4.4% / 58% | +4.2% / 73% |
| 0.7 | +5.9% / 87% | +5.9% / 87% | +11.2% / 69% | +10.1% / 69% | +5.0% / 82% | +3.6% / 47% | +2.2% / 83% | +2.5% / 88% | +4.5% / 58% | +4.8% / 81% |
| 0.8 | +6.2% / 87% | +6.2% / 87% | +10.8% / 71% | +10.4% / 70% | +5.2% / 82% | +3.7% / 47% | +2.9% / 78% | +2.5% / 88% | +5.1% / 54% | +4.8% / 81% |
| 0.9 | +6.6% / 9% | +6.4% / 87% | +11.8% / 70% | +11.1% / 69% | +7.7% / 48% | +5.8% / 91% | +3.2% / 82% | +3.0% / 83% | +5.3% / 57% | +4.7% / 63% |

## 3. Reading

- **The uncertainty points at the errors, moderately**: over the sheet, the share of BD TOPO road missed rises from 21.5% to 34.6% (x1.6) in the 20% most uncertain cells, and 45% of the exercises' gap points fall in those cells (20% if unrelated). It is computed without labels, so it can flag weak areas on a new sheet. It is highest in towns (Chalons, Verdun, Bar-le-Duc, Vitry, Saint-Dizier) and along road edges, so it also follows road density.
- **The routes already run on confident roads**: even at a threshold of 0.8, less than 0.5 km of a route is below it. The probability penalty steers between existing roads; it cannot create the roads missing from the traced network, which cause most mismatches (`prc_mismatch.md`).
- **A threshold of 0.7 is the useful setting**: route overlap with BD TOPO 73.7% -> 75.0% and with OSM 73.2% -> 74.3%, for +0.5 point of length. At 0.6 nothing changes (107 km of network below it); at 0.9 (811 km below it) routes detour around too much and overlap falls to 66% / 59%.
- **Next lever**: a region-level penalty from the uncertainty cells (label-free), and repairing the traced network where the uncertainty is high (bridge gaps, hysteresis threshold).

# docs

## Overview

Design and reference documents for COA-Net, and the figures they use. This folder has no source code of its own; the only code is the figure script in `figures/`.

- `COA-Net_methodology.md`: the methodology and design of COA-Net. Revised on 3 Nov 2026 after the organisers' answer: the doctrine is a set of textual documents handed out at the event, and orders are multimodal documents like the preparation exercise PDF. The revision adds document intake, the doctrine-set compiler with sign-off, order understanding with a readback, the sketch reader (L9), and the matching hypotheses, risks and event-day plan. A second revision the same day deploys the model locally: one open model on a machine the team controls, never a hosted API, with the doctrine method unchanged (no fine-tuning, no doctrine examples in the prompts). It adds section 6.5 (machines, serving, checks, offline use), section 11.7 (the client measured against a stand-in server), ablation A16 and risks R28 to R30. The commands are in `../deploy/README.md`.
- `benchmark_100_seeds.md`: output of the synthetic benchmark over 100 seeds.
- `hackathon_brief.md`: the organisers' AMIAD hackathon brief, copied word for word in French.
- `image_input_method.md`: the image-input method (9 Nov 2026): the road network read from the scanned IGN maps, with the shapefiles kept for sanity checks; method, parameters, measured results on Scenario 1 and limits.
- `corrector_route_impact.md`: what the learned corrector of the image network changes in the routes (planner, exercise 1, 405 origin-destination pairs).
- `real_bench_results.md`: the Scenario 1 order and the 100-seed benchmark run on BD TOPO, the image network and the image network with the corrector (`coa-net real-bench`).
- `prc_unet_and_pdf_exercises.md`, `prc_sweep_results.md`, `prc_mismatch.md`, `prc_mix_results.md`, `prc_uncertainty.md`, `prc_inference.md`, `prc_robust_results.md`, `prc_retrace_results.md`: the PRC U-Net (also trained with Scenario 1 as a second sheet) (fine-tuning, sweep, final model), the PDF route exercises (with and without BD TOPO / OSM), and where the PRC routes differ from BD TOPO and OSM.
- `real_data_validation.md`: inspection of the organisers' Scenario 1 data, the changes it required, and the measured results of exercise 1 and of a full plan with incidents on that map.

## Subdirectories & Boundaries

- figures: [./figures/README.md](./figures/README.md) - figure images and `make_figures.py`, which redraws them from the `coa_net` package and from `coa-net bench` output.

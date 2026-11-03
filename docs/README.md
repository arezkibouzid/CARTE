# docs

## Overview

Design and reference documents for COA-Net, and the figures they use. This folder has no source code of its own; the only code is the figure script in `figures/`.

- `COA-Net_methodology.md`: the methodology and design of COA-Net. Revised on 3 Nov 2026 after the organisers' answer: the doctrine is a set of textual documents handed out at the event, and orders are multimodal documents like the preparation exercise PDF. The revision adds document intake, the doctrine-set compiler with sign-off, order understanding with a readback, the sketch reader (L9), and the matching hypotheses, risks and event-day plan.
- `benchmark_100_seeds.md`: output of the synthetic benchmark over 100 seeds.
- `hackathon_brief.md`: the organisers' AMIAD hackathon brief, copied word for word in French.
- `real_data_validation.md`: inspection of the organisers' Scenario 1 data, the changes it required, and the measured results of exercise 1 and of a full plan with incidents on that map.

## Subdirectories & Boundaries

- figures: [./figures/README.md](./figures/README.md) - figure images and `make_figures.py`, which redraws them from the `coa_net` package and from `coa-net bench` output.

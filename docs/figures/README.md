# docs/figures

## Overview

Figures used by `docs/COA-Net_methodology.md` and `README.md`, plus the script that regenerates them. `make_figures.py` draws the architecture and incident-loop diagrams as SVG. It also runs the synthetic demo to draw the network maps and the COA chart. If given the markdown output of `coa-net bench`, it adds the benchmark chart.

## Interface

- `make_figures.py:main(argv=None)` is the CLI entry point. It takes:
  - `--out`: the output folder, default `docs/figures`.
  - `--bench`: the markdown written by `coa-net bench --out`. When given, the script adds `benchmark`.
- `make_figures.py:architecture()` and `make_figures.py:incident_loop(timings=None)` return the diagram SVGs.
- `make_figures.py:demo_figures()` builds the demo maps and the COA chart by running `COANet.from_graph(synthetic_theatre(0).G, sim_reps=50)`.
- `make_figures.py:parse_bench(md)` reads the first table of the `coa-net bench` output. `make_figures.py:bench_chart(md)` draws it.
- `make_figures.py:Canvas` is a small SVG builder. `page(svg, width)` and `captioned(title, svg)` wrap SVGs in HTML.
- Generated files: `benchmark.png`, `coa-chart.png`, `coa-net-architecture.png`, `demo-network-before.png`, `demo-network-after-I1.png`, `demo-network-after-I2.png`, `incident-loop.png`, `road-movement-graph.png`, `survivability.png`.

## Invariants

- Diagram colour code: blue boxes are language-model steps, grey boxes are symbolic steps, dashed boxes are inputs [make_figures.py module docstring; `BLUE`, `GREY`].
- The demo figures use the synthetic theatre with seed 0, so they are reproducible [make_figures.py:demo_figures].
- PNG rendering is optional. Without the `playwright` package, only the HTML pages are written and a notice is printed to stderr [make_figures.py:main].

## Side Effects

- Writes one HTML page per figure into `--out`. With `playwright` and Chromium installed, it also writes the matching PNG [make_figures.py:main].
- Reads the `--bench` markdown file when given [make_figures.py:main].
- Imports `coa_net` (`baselines`, `parsing`, `pipeline`, `refine`, `render`, `scenarios`) and runs a full plan and refinement in memory [make_figures.py:demo_figures].

## Verification

- Run: `python docs/figures/make_figures.py --out docs/figures`

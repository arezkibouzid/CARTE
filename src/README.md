# src

## Overview

Source root of the `coa-net` distribution. It uses the setuptools `src` layout (`[tool.setuptools.packages.find] where = ["src"]`) and holds one package.

## Subdirectories & Boundaries

- coa_net: [./coa_net/README.md](./coa_net/README.md) - the COA-Net planner. It runs stages S0 to S10: theatre graph, requirements, order and intent, candidate routes, criticality, CP-SAT network design, refinement, CP-SAT movement credits, verification and anticipation, COA briefing, and the incident loop. Its public API is `COANet` and `COA`, and its CLI is `coa-net`.

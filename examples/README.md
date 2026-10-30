# examples

## Overview

Runnable usage examples and sample inputs for the `coa_net` package. `run_python_api.py` is the smallest end-to-end use of the Python API on the synthetic theatre: plan, choose a course of action (COA), inject one incident, write a report. `order_brittany.txt` and `places_brittany.json` are inputs for running the CLI on a real map extract (see the root `README.md`).

## Interface

- `run_python_api.py` (script, no importable symbols). It calls, in order:
  - `coa_net.scenarios.synthetic_theatre(seed=0)` and `coa_net.COANet.from_graph(theatre.G, sim_reps=50)`
  - `COANet.plan(sample_text("sample_order.txt"))`, which returns the list of COAs
  - `session.intent.describe()`, `COA.report.summary()`, `COA.playbooks`, `session.comparison`
  - `session.choose()`, which picks the COA built from the confirmed intent order by default
  - `session.incident(text, time_min=...)`, which returns an outcome with `mode`, `playbook`, `seconds` and `frago`
  - `session.report("outputs/api")`
- `order_brittany.txt`: a sample logistic order in plain text.
- `places_brittany.json`: a gazetteer in the form `{"name": [lon, lat]}`, which lets orders and incidents refer to named places.

## Invariants

- The script runs without a language model. Pass `llm=coa_net.llm.from_env()` to `COANet.from_graph` to use one [run_python_api.py:COANet.from_graph].
- The incident time is given in minutes after midnight (`time_min=6 * 60 + 5` for 06:05) [run_python_api.py:session.incident].
- The script must be run with `coa_net` installed (`pip install -e .`) or with `src` on `PYTHONPATH`.

## Side Effects

- Writes a report directory under `outputs/api/` relative to the current working directory [run_python_api.py:session.report].
- Prints the intent, the COA summaries, the comparison, the incident outcome and the FRAGO to stdout.

## Verification

- Run: `python examples/run_python_api.py`
- Run: `pytest`

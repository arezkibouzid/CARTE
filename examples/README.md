# examples

## Overview

Runnable usage examples and sample inputs for the `coa_net` package. `run_python_api.py` is the smallest end-to-end use of the Python API on the synthetic theatre: plan, choose a course of action (COA), inject one incident, write a report. `order_brittany.txt` and `places_brittany.json` are inputs for running the CLI on a real map extract (see the root `README.md`).

## Interface

- `run_python_api.py` (script, no importable symbols). It calls, in order:
  - `coa_net.scenarios.synthetic_theatre(seed=0)` and `coa_net.COANet.from_graph(theatre.G, sim_reps=50)`
  - `COANet.plan(sample_text("sample_order.txt"))`, which returns the list of COAs, default COA first [src/coa_net/pipeline.py:COANet.plan]. `sample_order.txt` is package data read from `src/coa_net/data/` [src/coa_net/scenarios.py:sample_text], not a file in this folder.
  - `session.intent.describe()` [src/coa_net/intent.py:IntentResult.describe], `COA.report.summary()`, `COA.playbooks`, and the `session.comparison` string attribute [src/coa_net/pipeline.py:COANet.comparison]
  - `session.choose()`, which picks the COA built from the confirmed intent order by default [src/coa_net/pipeline.py:COANet.choose]
  - `session.incident(text, time_min=...)`, which returns an `IncidentOutcome` with `mode`, `playbook`, `seconds` and `frago` [src/coa_net/pipeline.py:COANet.incident]
  - `session.report("outputs/api")`, which returns the path of `report.html`; the script prints it resolved [src/coa_net/pipeline.py:COANet.report]
- `order_brittany.txt`: a sample logistic order in plain text (three convoys between Rennes, Redon, Montfort, Guer and Ploermel), used with `coa-net plan --order` [src/coa_net/cli.py:--order].
- `places_brittany.json`: a gazetteer in the form `{"name": [lon, lat]}`, which lets orders and incidents refer to named places; passed with `coa-net plan --places` [src/coa_net/cli.py:--places].
- `places_scenario1.json`: the same form, with 15 town positions read by OCR on the organisers' Scenario 1 1:100 000 scan (`coa_net.scanmap.places_from_labels`).
- `order_scenario1.txt`: a test order on the Scenario 1 map (three convoys between those towns), written for the validation in `docs/real_data_validation.md`; it is not an ETLO order.

## Invariants

- The script runs without a language model. Pass `llm=coa_net.llm.from_env()` to `COANet.from_graph` to use one [run_python_api.py:COANet.from_graph].
- The incident time is given in minutes after midnight (`time_min=6 * 60 + 5` for 06:05) [run_python_api.py:session.incident].
- `session.incident` calls `session.choose()` first if no COA has been chosen [src/coa_net/pipeline.py:COANet.incident].
- Each sample order is paired with its gazetteer: every origin and destination named in `order_brittany.txt` is a key of `places_brittany.json`, and likewise for `order_scenario1.txt` and `places_scenario1.json`. Keep the pairs in sync when editing either file.
- The script must be run with `coa_net` installed (`pip install -e .`) or with `src` on `PYTHONPATH`.

## Side Effects

- Creates `outputs/api/` relative to the current working directory and writes `report.html`, `panel_NN.svg`, `network.geojson`, `facts.md`, `fragos.txt` and `metrics.json` into it [src/coa_net/pipeline.py:COANet.report]. `outputs/` is ignored by `.gitignore`.
- Prints the intent, the COA summaries, the comparison, the incident outcome and the FRAGO to stdout.

## Verification

- Run: `python examples/run_python_api.py`
- Run: `pytest`

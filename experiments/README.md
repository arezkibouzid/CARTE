# Experiments

Reproducible ablations and stress tests of the AMIAD pipeline. Results: [`INDEX.md`](INDEX.md) (all runs) and
[`../docs/technical_overview.md`](../docs/technical_overview.md) (what they show).

| script | what | where |
|---|---|---|
| `routing_ablation.py` | network source, route cost (threshold, λ), explanation cost on the 10 PDF exercises | CPU (`run_cpu.sh`) |
| `stress_test.py` | distance, area, convoys, waypoints, zones, parameter combinations, cores, incomplete / uncertain data | CPU (`run_cpu.sh`) |
| `imagery_ablation.py` | U-Net zero-shot vs fine-tuned × 9 kinds of input imagery, out of fold | 1 GPU (`run_gpu.sh`) |
| `index.py` | rewrites `INDEX.md` from `runs/` | anywhere |
| `common.py` | run folder, environment, resources and energy, never overwriting | — |

```
sbatch -J routing_ablation experiments/run_cpu.sh experiments/routing_ablation.py [--groups network cost explain] [--exercises 1 2]
sbatch -J stress_test      experiments/run_cpu.sh experiments/stress_test.py [--tests convoys degraded] [--quick]
sbatch -J imagery_ablation experiments/run_gpu.sh experiments/imagery_ablation.py [--folds 0 1] [--imagery original blur]
.venv/bin/python experiments/index.py
```

Every run writes `runs/<date-time>_<name>_<commit>[_k]/` with `config.json`, `environment.json`, `log.txt`,
`metrics.json`, `metrics.csv`, `resources.json`, figures and, when the code is not committed, `code.diff`. Runs are
never overwritten or deleted, failed and quick ones included. Analyses derived from a run go to `analysis/`, naming
their source run. Slurm logs: `slurm_logs/`.

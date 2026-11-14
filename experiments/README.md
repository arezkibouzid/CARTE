# Experiments

Reproducible ablations and stress tests of the AMIAD pipeline. Results: [`INDEX.md`](INDEX.md) (all runs) and
[`../docs/technical_overview.md`](../docs/technical_overview.md) (what they show).

| script | what | where |
|---|---|---|
| `routing_ablation.py` | network source, route cost (threshold, λ), explanation cost on the 10 PDF exercises | CPU (`run_cpu.sh`) |
| `stress_test.py` | distance, area, convoys, waypoints, zones, parameter combinations, cores, incomplete / uncertain data | CPU (`run_cpu.sh`) |
| `imagery_ablation.py` | U-Net zero-shot vs fine-tuned × 9 kinds of input imagery, out of fold | 1 GPU (`run_gpu.sh`) |
| `context_stress.py` | many orders, chains of FRAGOs, token budgets, conflicts: what the chat's context keeps | CPU + the app's model |
| `llm_compare.py` | one model on long contexts: retrieval among 10–400 orders, chains, conflicts, written orders, at 6k–100k tokens | `run_llm_compare.sh` (serves the model on 1 GPU) or CPU + the app's model |
| `rag_reproduce.py` | doctrine rules (`amiad-doctrine-rag/`): rebuilds the extraction, quote check and rule finder and compares them with the committed files; runs the pipeline's tests; checks the app uses those rules | CPU (1 min) |
| `index.py` | rewrites `INDEX.md` from `runs/` | anywhere |
| `common.py` | run folder, environment, resources and energy, never overwriting | — |

```
sbatch -J routing_ablation experiments/run_cpu.sh experiments/routing_ablation.py [--groups network cost explain] [--exercises 1 2]
sbatch -J stress_test      experiments/run_cpu.sh experiments/stress_test.py [--tests convoys degraded] [--quick]
sbatch -J cmp experiments/run_llm_compare.sh ~/extraction/models/<model dir> <served name> [--budgets 6000,32000,100000]
sbatch -J imagery_ablation experiments/run_gpu.sh experiments/imagery_ablation.py [--folds 0 1] [--imagery original blur]
.venv/bin/python experiments/index.py
```

Every run writes `runs/<date-time>_<name>_<commit>[_k]/` with `config.json`, `environment.json`, `log.txt`,
`metrics.json`, `metrics.csv`, `resources.json`, figures and, when the code is not committed, `code.diff`. Runs are
never overwritten or deleted, failed and quick ones included. Analyses derived from a run go to `analysis/`, naming
their source run. Slurm logs: `slurm_logs/`.

## Language model: same size, longer context?

Qwen3-VL-8B-Instruct (8.8 B, native window 262,144) against Qwen2.5-7B-Instruct-1M (7.6 B, trained for 1 M tokens,
text only), both served the same way (vLLM 0.11, bfloat16, 262,144-token window, greedy, JSON grammar) on one H100,
same questions (`llm_compare.py`). Counted on the model's first answer, before the app's checks, where the asked order
was in the context:

| | Qwen3-VL-8B | Qwen2.5-7B-1M |
|---|---|---|
| retrieval: one order's radio frequency among 10–400 orders (up to 112,000 real tokens) | 37/37 | 31/37 (0/3 at 112k) |
| value in force at the end of a chain of 5–25 FRAGOs | 9/9, never the replaced value | 9/9, once the replaced value |
| conflict between annexes reported, not settled | 3/3 | 0/3 |
| written orders → scenario fields | 10/10 | 9/10 |
| mean seconds per chat answer | 4.6 | 2.5 |

Runs `20261114-071816_llm_compare` and `20261114-072142_llm_compare`. The 1 M model was served without its dual-chunk
attention (only used past 262,144 tokens; vLLM 0.11 fails on it): a copy of its config without that block, same weights.
Kept: Qwen3-VL-8B (it also reads the order pages as images, which the 1 M model cannot). What lost answers was the app's
6,000-token budget (from 100 orders the asked order was left out: 7 questions of 15 lost) and a check that rejected answers citing an order's own
fragment: the chat now cites fragments (`cited_facts`), the model serves 131,072 tokens and the budget is 100,000.

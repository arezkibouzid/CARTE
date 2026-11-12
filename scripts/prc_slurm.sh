#!/bin/bash
#SBATCH -p low
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH -t 03:00:00
# usage: sbatch [--array=0-3] scripts/prc_slurm.sh zeroshot|finetune OUT
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
CMD=$1; OUT=$2; shift 2
ARGS=(--out "$OUT" --prc-repo "$HOME/ext/ProbRoadClass-DeepLearning" --weights "$HOME/ext/prc_weights/prc_binary.pt")
[ "$CMD" = finetune ] && ARGS+=(--fold "$SLURM_ARRAY_TASK_ID")
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
.venv/bin/python -u scripts/prc_unet.py "$CMD" "${ARGS[@]}" "$@"

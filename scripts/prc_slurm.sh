#!/bin/bash
#SBATCH -p low
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH -t 08:00:00
# usage: sbatch [--array=0-3] scripts/prc_slurm.sh zeroshot|finetune OUT
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
CMD=$1; OUT=$2; shift 2
ARGS=(--out "$OUT" --prc-repo "$HOME/ext/ProbRoadClass-DeepLearning" --weights "$HOME/ext/prc_weights/prc_binary.pt")
{ [ "$CMD" = finetune ] || [ "$CMD" = repredict ]; } && [ -n "${SLURM_ARRAY_TASK_ID:-}" ] && ARGS+=(--fold "$SLURM_ARRAY_TASK_ID")
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
# the venv's torch is built for CUDA 13; nodes whose driver stops at CUDA 12.x use .venv128 (torch for CUDA 12.8)
CUDA_MAJOR=$(nvidia-smi | sed -n 's/.*CUDA Version: *\([0-9]*\).*/\1/p' | head -1)
PY=.venv/bin/python
[ "${CUDA_MAJOR:-13}" -lt 13 ] && PY=.venv128/bin/python
echo "node $(hostname), driver CUDA $CUDA_MAJOR, python $PY"
$PY -u scripts/prc_unet.py "$CMD" "${ARGS[@]}" "$@"

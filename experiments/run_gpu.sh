#!/bin/bash
#SBATCH -p low
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=96G
#SBATCH -t 04:00:00
#SBATCH -o experiments/slurm_logs/%x-%j.out
# usage: sbatch -J NAME experiments/run_gpu.sh experiments/SCRIPT.py [args]   (one H100)
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
CUDA_MAJOR=$(nvidia-smi | sed -n 's/.*CUDA Version: *\([0-9]*\).*/\1/p' | head -1)
PY=.venv/bin/python
[ "${CUDA_MAJOR:-13}" -lt 13 ] && PY=.venv128/bin/python   # nodes whose driver stops at CUDA 12.x
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK
echo "node $(hostname), driver CUDA $CUDA_MAJOR, python $PY, job $SLURM_JOB_ID"
cd experiments && ../$PY -u "$(basename "$1")" "${@:2}"

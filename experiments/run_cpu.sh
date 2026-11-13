#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH -t 04:00:00
#SBATCH -o experiments/slurm_logs/%x-%j.out
# usage: sbatch -J NAME experiments/run_cpu.sh experiments/SCRIPT.py [args]   (CPU only: 16 threads, no GPU)
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK OPENBLAS_NUM_THREADS=$SLURM_CPUS_PER_TASK CUDA_VISIBLE_DEVICES=""
echo "node $(hostname), job $SLURM_JOB_ID, $SLURM_CPUS_PER_TASK CPUs"
cd experiments && ../.venv/bin/python -u "$(basename "$1")" "${@:2}"

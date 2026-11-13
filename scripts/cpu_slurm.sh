#!/bin/bash
#SBATCH -p low
#SBATCH --cpus-per-task=24
#SBATCH --mem=128G
#SBATCH -t 06:00:00
# usage: sbatch scripts/cpu_slurm.sh <coa-net arguments...>   (CPU job; threads pinned to the allocation)
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=$SLURM_CPUS_PER_TASK MKL_NUM_THREADS=$SLURM_CPUS_PER_TASK OPENBLAS_NUM_THREADS=$SLURM_CPUS_PER_TASK
.venv/bin/python -u -m coa_net "$@"

#!/bin/bash
# The language model on CPU only (no GPU requested): deploy/llm_launch.py --device cpu, Qwen3-VL-8B as on the GPU.
# With llama.cpp installed (llama-server on the PATH, AMIAD_LLAMA_SERVER, or ~/ext/llama.cpp/build/bin): the official
# 4-bit build with its image encoder; otherwise deploy/cpu_llm_server.py (transformers, bfloat16, much slower).
# Writes the endpoint file the app reads, so the app needs no other change.
#   sbatch deploy/serve_llm_cpu.sbatch                                   (replaces the GPU server for the app)
#   AMIAD_ENDPOINT_FILE=outputs/orders/llm_endpoints_cpu.json sbatch deploy/serve_llm_cpu.sbatch   (side by side, tests)
# Without Slurm: interface/run.sh --llm (the same launcher, on this machine)
#SBATCH --job-name=amiad-llm-cpu
#SBATCH --partition=low
#SBATCH --cpus-per-task=32
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=outputs/orders/llm-cpu-%j.log
set -eu
REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
cd "$REPO"
export CUDA_VISIBLE_DEVICES=""
exec .venv/bin/python -u deploy/llm_launch.py --device cpu --host 0.0.0.0 \
  --endpoint-file "${AMIAD_ENDPOINT_FILE:-outputs/orders/llm_endpoints.json}"

#!/bin/bash
#SBATCH -p low
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=96G
#SBATCH -t 03:00:00
#SBATCH -o experiments/slurm_logs/%x-%j.out
# One model on one H100 (vLLM, 262,144-token window, weights on disk), then experiments/llm_compare.py against it.
#   sbatch -J llm_compare experiments/run_llm_compare.sh ~/extraction/models/Qwen2.5-7B-Instruct-1M Qwen/Qwen2.5-7B-Instruct-1M [args]
set -euo pipefail
cd "$SLURM_SUBMIT_DIR"
MODEL_DIR=$1; NAME=$2; shift 2
VLLM="${AMIAD_VLLM:-$HOME/extraction/.venv-vllm/bin/vllm}"
PORT=$(.venv/bin/python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
MM=()
[[ "$NAME" == *VL* ]] && MM=(--limit-mm-per-prompt '{"image":8}' --mm-processor-kwargs '{"max_pixels":2508800}')
# Qwen2.5-1M: served from a copy of its config without dual-chunk attention (only needed past its native 262,144 tokens;
# vLLM 0.11 fails on it): see the comparison README
echo "node $(hostname), job $SLURM_JOB_ID, model $NAME, port $PORT"
"$VLLM" serve "$MODEL_DIR" --served-model-name "$NAME" --host 127.0.0.1 --port "$PORT" --dtype bfloat16 \
  --max-model-len 262144 --max-num-seqs 4 --gpu-memory-utilization 0.85 "${MM[@]}" \
  --structured-outputs-config '{"backend":"xgrammar","disable_any_whitespace":true,"disable_fallback":true}' \
  --generation-config vllm > "experiments/slurm_logs/vllm-$SLURM_JOB_ID.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT
for i in $(seq 1 300); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break
  kill -0 $PID 2>/dev/null || { echo "vLLM stopped"; tail -30 "experiments/slurm_logs/vllm-$SLURM_JOB_ID.log"; exit 1; }
  sleep 3
done
nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv
export COA_LLM_BASE_URL="http://127.0.0.1:$PORT/v1" COA_LLM_MODEL="$NAME" COA_LLM_TIMEOUT=600
cd experiments && ../.venv/bin/python -u llm_compare.py "$@"
nvidia-smi --query-gpu=memory.used --format=csv

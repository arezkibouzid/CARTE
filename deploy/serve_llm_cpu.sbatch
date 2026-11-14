#!/bin/bash
# The language model on CPU only (no GPU requested): deploy/cpu_llm_server.py with Qwen3-VL-8B, the same model, JSON
# grammar and image input as the GPU server, one request at a time (AMIAD_CPU_MODEL=Qwen3-VL-4B-Instruct: smaller,
# faster, less reliable). Writes the endpoint file the app reads, so the app needs no other change.
#   sbatch deploy/serve_llm_cpu.sbatch                                   (replaces the GPU server for the app)
#   AMIAD_ENDPOINT_FILE=outputs/orders/llm_endpoints_cpu.json sbatch deploy/serve_llm_cpu.sbatch   (side by side, tests)
# Without Slurm: python deploy/cpu_llm_server.py --port 8010, then COA_LLM_BASE_URL=http://127.0.0.1:8010/v1 interface/run.sh
#SBATCH --job-name=amiad-llm-cpu
#SBATCH --partition=low
#SBATCH --cpus-per-task=32
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=outputs/orders/llm-cpu-%j.log
set -eu
REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
MODELS="${AMIAD_MODELS:-$HOME/extraction/models}"
PY="${AMIAD_VLLM_PY:-$HOME/extraction/.venv-vllm/bin/python}"   # torch, transformers, xgrammar, fastapi, uvicorn
EP="${AMIAD_ENDPOINT_FILE:-outputs/orders/llm_endpoints.json}"
export CUDA_VISIBLE_DEVICES=""
cd "$REPO"
HOST=$(hostname -f)
PORT=$("$PY" -c 'import socket; s=socket.socket(); s.bind(("0.0.0.0",0)); print(s.getsockname()[1]); s.close()')
CPU_MODEL="${AMIAD_CPU_MODEL:-Qwen3-VL-8B-Instruct}"
"$PY" -u deploy/cpu_llm_server.py --model "$MODELS/$CPU_MODEL" --name "Qwen/$CPU_MODEL" --host 0.0.0.0 --port "$PORT" \
  --threads "${SLURM_CPUS_PER_TASK:-32}" --max-len 32768 &
PID=$!
trap 'kill $PID 2>/dev/null || true; rm -f "$EP"' EXIT
for i in $(seq 1 200); do
  curl -sf "http://127.0.0.1:$PORT/health" | grep -q true && break
  kill -0 $PID 2>/dev/null || { echo "server stopped"; exit 1; }
  sleep 3
done
printf '{"job": "%s", "device": "cpu", "extraction": {"base_url": "http://%s:%s/v1", "model": "Qwen/%s"}, "explanation": {"base_url": "http://%s:%s/v1", "model": "Qwen/%s"}}\n' \
  "$SLURM_JOB_ID" "$HOST" "$PORT" "$CPU_MODEL" "$HOST" "$PORT" "$CPU_MODEL" > "$EP"
echo "ready: $(cat "$EP")"
wait

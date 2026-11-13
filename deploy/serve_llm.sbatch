#!/bin/bash
# Local language models for the orders workflow, on one GPU of the cluster (vLLM, weights on disk, nothing leaves the network).
# One model for every role: Qwen3-VL-8B-Instruct, the model the extraction bundle was validated with (images + schema),
# also serves the rule explanations and the chatbot. Qwen3-VL-4B was measured on the same explanations and questions:
# as fast here, but it misstated rule contents and statuses where the 8B did not (see interface/backend/orders.py).
# Writes the endpoint to outputs/orders/llm_endpoints.json; the app reads that file (or COA_LLM_BASE_URL).
#   sbatch deploy/serve_llm.sbatch            (stop: scancel <job>)
#SBATCH --job-name=amiad-llm
#SBATCH --partition=low
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --time=08:00:00
#SBATCH --output=outputs/orders/llm-%j.log
set -eu
REPO="${SLURM_SUBMIT_DIR:-$(pwd)}"
MODELS="${AMIAD_MODELS:-$HOME/extraction/models}"
VLLM="${AMIAD_VLLM:-$HOME/extraction/.venv-vllm/bin/vllm}"
PY="${AMIAD_PY:-$HOME/extraction/.venv-llm/bin/python}"
free_port() { "$PY" -c 'import socket; s=socket.socket(); s.bind(("0.0.0.0",0)); print(s.getsockname()[1]); s.close()'; }
HOST=$(hostname -f)  # the name other cluster nodes resolve (the first interface address may be internal to the node)
P8=$(free_port)
cd "$REPO"
"$VLLM" serve "$MODELS/Qwen3-VL-8B-Instruct" --served-model-name Qwen/Qwen3-VL-8B-Instruct --host 0.0.0.0 --port "$P8" \
  --dtype bfloat16 --max-model-len 32768 --max-num-seqs 4 --gpu-memory-utilization 0.50 \
  --limit-mm-per-prompt '{"image":8}' --mm-processor-kwargs '{"max_pixels":2508800}' \
  --structured-outputs-config '{"backend":"xgrammar","disable_any_whitespace":true,"disable_fallback":true}' \
  --generation-config vllm > "outputs/orders/vllm-8b-$SLURM_JOB_ID.log" 2>&1 &
PID8=$!
wait_up() {  # $1 port, $2 pid: wait until the server answers (vLLM loads and compiles the model first)
  for i in $(seq 1 300); do
    curl -sf "http://127.0.0.1:$1/health" >/dev/null && return 0
    kill -0 "$2" 2>/dev/null || { echo "vLLM stopped; see outputs/orders/vllm-*-$SLURM_JOB_ID.log"; exit 1; }
    sleep 3
  done
  echo "vLLM not ready after 15 minutes"; exit 1
}
trap 'kill $PID8 2>/dev/null || true; rm -f outputs/orders/llm_endpoints.json' EXIT
wait_up "$P8" "$PID8"
printf '{"job": "%s", "extraction": {"base_url": "http://%s:%s/v1", "model": "Qwen/Qwen3-VL-8B-Instruct"}, "explanation": {"base_url": "http://%s:%s/v1", "model": "Qwen/Qwen3-VL-8B-Instruct"}}\n' \
  "$SLURM_JOB_ID" "$HOST" "$P8" "$HOST" "$P8" > outputs/orders/llm_endpoints.json
echo "ready: $(cat outputs/orders/llm_endpoints.json)"
wait

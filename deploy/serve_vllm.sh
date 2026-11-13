#!/usr/bin/env bash
# Serve Mistral Small 3.2 24B with vLLM on one H100 (80 GB), for this machine only (127.0.0.1:8000).
# The model card gives about 55 GB of GPU memory in bf16 and asks for vLLM >= 0.9.1; the licence is Apache 2.0.
# Offline: download the model folder before the event (README.md) and run with MODEL=/path/to/folder HF_HUB_OFFLINE=1.
set -euo pipefail
MODEL="${MODEL:-mistralai/Mistral-Small-3.2-24B-Instruct-2506}"
NAME="${NAME:-mistral-small-3.2}"      # the name COA-Net asks for (COA_LLM_MODEL)
PORT="${PORT:-8000}"
MAX_LEN="${MAX_LEN:-16384}"            # no COA-Net call exceeds about 4.5k tokens (methodology, section 6.3)
GPU_UTIL="${GPU_UTIL:-0.90}"
exec vllm serve "$MODEL" \
  --tokenizer-mode mistral --config-format mistral --load-format mistral \
  --served-model-name "$NAME" \
  --host 127.0.0.1 --port "$PORT" \
  --max-model-len "$MAX_LEN" \
  --gpu-memory-utilization "$GPU_UTIL"

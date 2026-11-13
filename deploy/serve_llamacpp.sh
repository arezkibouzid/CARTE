#!/usr/bin/env bash
# Serve Mistral Small 3.2 24B as a 4-bit GGUF with llama.cpp (24 GB GPU, Apple Silicon, or CPU), on 127.0.0.1:8000.
# Q4_K_M is 14.3 GB; the vision projector (mmproj) comes with it, so the sketch reader (L9) works.
# Offline: set GGUF and MMPROJ to files downloaded before the event (README.md).
set -euo pipefail
PORT="${PORT:-8000}"
CTX="${CTX:-16384}"                    # context; no COA-Net call exceeds about 4.5k tokens
NGL="${NGL:-99}"                       # layers on the GPU; NGL=0 on a machine without GPU
ALIAS="${ALIAS:-mistral-small-3.2}"    # the model name the server lists (same as serve_vllm.sh); COA-Net takes it
if [ -n "${GGUF:-}" ]; then
  exec llama-server -m "$GGUF" --mmproj "${MMPROJ:?set MMPROJ to the mmproj file that goes with the model}" \
    --jinja -c "$CTX" -ngl "$NGL" --alias "$ALIAS" --host 127.0.0.1 --port "$PORT"
fi
# first run: downloads the model and its mmproj file from Hugging Face into the llama.cpp cache
exec llama-server -hf "${HF_REPO:-unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF:Q4_K_M}" \
  --jinja -c "$CTX" -ngl "$NGL" --alias "$ALIAS" --host 127.0.0.1 --port "$PORT"

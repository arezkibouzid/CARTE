# deploy

## CARTE: the local language model, GPU or CPU

One model everywhere, **Qwen3-VL-8B-Instruct** (Apache 2.0, reads images), chosen by `llm_launch.py` from what the
machine has. It writes `outputs/orders/llm_endpoints.json`, the file the app reads.

| machine | how the model runs | started by |
|---|---|---|
| GPU (H100 here) | vLLM, bfloat16, 131,072-token window | `sbatch deploy/serve_llm.sbatch`, or `interface/run.sh --llm` |
| CPU only, llama.cpp installed | `llama-server`, official 4-bit build `Qwen/Qwen3-VL-8B-Instruct-GGUF` (Q4_K_M, 5.0 GB) + image encoder (mmproj F16, 1.2 GB), 32,768 tokens: a written order in 7 s, a chat answer in 50–60 s on 32 cores | `sbatch deploy/serve_llm_cpu.sbatch`, or `interface/run.sh --llm` |
| CPU only, no llama.cpp | `cpu_llm_server.py` (transformers, bfloat16): a written order in 22 s, a chat answer in about 9 min on 32 cores, so the chat uses its deterministic answer | same, automatic fallback |

Missing weights are downloaded from Hugging Face into `$AMIAD_MODELS` (default `~/extraction/models`).
`--cpu-backend llamacpp|transformers` (or `AMIAD_CPU_BACKEND`) forces a CPU path; `AMIAD_CPU_MODEL=Qwen/Qwen3-VL-4B-Instruct`
uses the smaller model on the transformers path.

**Install llama.cpp (CPU).** `llm_launch.py` looks for `llama-server` on the PATH, in `$AMIAD_LLAMA_SERVER`, or in
`~/ext/llama.cpp/build/bin/`. To build it on Linux (CMake 3.14+, a C++17 compiler; release tag of your choice):

```bash
git clone --depth 1 --branch <release tag> https://github.com/ggml-org/llama.cpp ~/ext/llama.cpp
cd ~/ext/llama.cpp
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DGGML_CUDA=OFF -DLLAMA_CURL=OFF
cmake --build build --target llama-server -j 16
```

(`pip install cmake ninja` if CMake is missing; on a Mac `brew install llama.cpp`.) Then
`python deploy/llm_launch.py --dry-run` should print `CPU: Qwen/Qwen3-VL-8B-Instruct with llama.cpp (Q4_K_M)`.

## Background: the hackathon setup (Mistral Small 3.2)

## Overview

How to run COA-Net's language model locally. Every model role (L1 to L9) goes to one open model, Mistral Small 3.2 24B (Apache 2.0, takes images), served on a machine the team controls through an OpenAI-compatible endpoint. No hosted API is needed, and doctrine and orders do not leave that machine.

The doctrine method does not change: S1 still unites the pattern compiler with L1's checked instances, reconciles them and asks for sign-off. Only the place where L1 runs is fixed, the local server. The model is used as it is, with no fine-tuning and no doctrine examples in the prompts, since no doctrine is available before the event.

| Machine | Model and size | Server | Images (L9) |
| --- | --- | --- | --- |
| The organisers' H100 (80 GB), at the event | Mistral Small 3.2 24B in bf16, about 55 GB of GPU memory (model card) | vLLM 0.9.1 or later: `serve_vllm.sh` | yes |
| A 24 GB GPU or an Apple Silicon Mac with 32 GB, before the event | the same model as a 4-bit GGUF file, Q4_K_M, 14.3 GB | llama.cpp: `serve_llamacpp.sh`; or Ollama | yes, with the mmproj file |
| A 32 to 48 GB GPU or a Mac with 48 GB or more | the 8-bit GGUF file, Q8_0, 25.1 GB | llama.cpp or Ollama | yes, with the mmproj file |
| A laptop without GPU, 32 GB of RAM | the Q4_K_M file on the CPU (`NGL=0`), slow | llama.cpp | yes, slowly |
| A 16 GB laptop, to rehearse | Mistral Small does not fit; Qwen3-8B, Q4_K_M, 5.03 GB (Apache 2.0, text only): `HF_REPO=Qwen/Qwen3-8B-GGUF:Q4_K_M ALIAS=qwen3-8b deploy/serve_llamacpp.sh` | llama.cpp | no: L9 uses position words |
| Any machine, no model | none | none | no: every role uses its fallback |

Sizes are those listed by the model card and the GGUF repositories. With a 16k context, the cache adds about 2.7 GB at 16 bits (estimate), so the 4-bit build fits a 24 GB GPU with its projector. Qwen3-8B's model card advises against greedy decoding, which COA-Net uses (temperature 0), so run `coa-net llm-check` before relying on it. Speeds have not been measured yet (methodology, section 6.3).

## Interface

- `serve_vllm.sh`: vLLM on `127.0.0.1:8000`, model name `mistral-small-3.2`. Variables: `MODEL` (repository or local folder), `NAME`, `PORT`, `MAX_LEN` (default 16384), `GPU_UTIL` (default 0.90).
- `serve_llamacpp.sh`: llama.cpp `llama-server` on `127.0.0.1:8000`. By default it downloads `unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF:Q4_K_M` with its vision projector. Variables: `GGUF` and `MMPROJ` (local files, for offline use), `HF_REPO`, `ALIAS` (the model name the server lists, default `mistral-small-3.2` as with vLLM), `PORT`, `CTX` (default 16384), `NGL` (layers on the GPU, default 99; 0 without a GPU).
- `coa-llm.env.example`: the `COA_LLM_*` variables COA-Net reads (`COA_LLM_API_KEY`, also read by `coa_net/llm.py:from_env`, is not in the template; set it only if the server requires a key). Copy it to `coa-llm.env`, then `set -a; . deploy/coa-llm.env; set +a`.
- `coa-net llm-check` (in the package): asks the server for its models, then checks a schema-constrained JSON answer, an image (for L9), the speed, and a validated call through `call_validated` (the path every role uses). It then prints a suggested `export COA_LLM_BASE_URL=… COA_LLM_MODEL=…` line (with `COA_LLM_JSON_SCHEMA=0` if the schema was not honoured). `--no-image` skips the image probe, and `--out FILE` writes the result, including every probe, as JSON. [coa_net/cli.py:cmd_llm_check] It exits with code 1 if the server does not answer or the JSON answer is wrong.

### Before the event (on a team machine)

```bash
# 0. the server software, kept for the event: llama.cpp's llama-server on a team machine (a release build from the
#    llama.cpp GitHub page, or `brew install llama.cpp` on a Mac); vLLM for the H100 (`pip install "vllm>=0.9.1"`, CUDA)
# 1. the model files, once, while there is a network (recent versions of huggingface_hub call the command `hf download`)
huggingface-cli download unsloth/Mistral-Small-3.2-24B-Instruct-2506-GGUF --include "*Q4_K_M*" "mmproj*" --local-dir models/ms32-gguf
# for the H100: bf16 weights, 24 billion parameters at 2 bytes, so about 48 GB; check the disk, the repository may hold two formats
huggingface-cli download mistralai/Mistral-Small-3.2-24B-Instruct-2506 --local-dir models/ms32

# 2. a server (llama.cpp here), then the check
GGUF=models/ms32-gguf/Mistral-Small-3.2-24B-Instruct-2506-Q4_K_M.gguf MMPROJ=models/ms32-gguf/mmproj-F16.gguf deploy/serve_llamacpp.sh &
set -a; . deploy/coa-llm.env; set +a
coa-net llm-check

# 3. the pipeline with the model
coa-net exercise --data DATA --pdf exercise_for_preparation.pdf --llm --out outputs/exercise1
```

Check the file names in `models/ms32-gguf` after the download: the repository lists them, and they may differ from the names above.

With Ollama instead of llama.cpp, pull a tag of `mistral-small3.2` that fits the GPU (the library lists vision and tools for it), then set `COA_LLM_BASE_URL=http://127.0.0.1:11434/v1` and leave `COA_LLM_MODEL` empty or give the tag. Run `coa-net llm-check` to see whether that server constrains the output with the schema. If it does not, set `COA_LLM_JSON_SCHEMA=0`.

### At the event (the organisers' H100)

```bash
# on the GPU server, with vLLM installed beforehand, from the folder downloaded beforehand, without network
MODEL=/path/to/models/ms32 HF_HUB_OFFLINE=1 deploy/serve_vllm.sh
# on the laptop that runs COA-Net: a tunnel, so that the model stays at 127.0.0.1 for COA-Net
ssh -N -L 8000:127.0.0.1:8000 user@gpu-server
coa-net llm-check
```

COA-Net can also run on the GPU server itself, with the same variables.

## Invariants

- **Local by default.** Without `COA_LLM_BASE_URL`, COA-Net calls `http://127.0.0.1:8000/v1`. An endpoint outside this machine and its private network is refused unless `COA_LLM_ALLOW_REMOTE=1`; the report then says the model was remote. [coa_net/llm.py:from_env, coa_net/llm.py:is_local_url]
- **Checked before the run.** `--llm` asks the server for its models first, and stops with advice if it does not answer. [coa_net/cli.py:_llm]
- **Fast failure during the run.** The connection times out after `COA_LLM_CONNECT_TIMEOUT` seconds (5), a call after `COA_LLM_TIMEOUT` (120). After the first connection error or timeout, the client stops calling the server for the rest of the run, so that every later role uses its deterministic fallback at once. The report and `metrics.json` say so. An HTTP error status or an unparsable answer does not mark the server down; later roles still call it. [coa_net/llm.py:OpenAICompatibleClient._post, coa_net/pipeline.py:COANet._model_line]
- **Same settings everywhere.** Temperature 0, schema-constrained output when the server accepts it, a plain JSON prompt otherwise; in both cases the answer's shape is checked against the schema before the role's own checks. The model card recommends 0.15 for chat; COA-Net only asks for typed records, so it keeps 0. [coa_net/llm.py:call_validated, coa_net/llm.py:schema_problems]
- **The servers listen on 127.0.0.1 only.** Another machine reaches them through an SSH tunnel. [serve_vllm.sh:--host, serve_llamacpp.sh:--host]
- **Both servers answer to the same name and port.** `NAME` (vLLM) and `ALIAS` (llama.cpp) both default to `mistral-small-3.2`, and `PORT` to 8000, matching the default `COA_LLM_BASE_URL` (`coa_net/llm.py:LOCAL_DEFAULT_URL`). Change them together with `COA_LLM_BASE_URL` / `COA_LLM_MODEL`. [serve_vllm.sh:NAME, serve_llamacpp.sh:ALIAS]
- **Local GGUF needs its projector.** When `GGUF` is set, `serve_llamacpp.sh` stops with an error unless `MMPROJ` is set too (`${MMPROJ:?...}`), so a local model never starts without the vision projector L9 needs. Both scripts run under `set -euo pipefail` and `exec` the server, so the script process becomes the server process. [serve_llamacpp.sh:MMPROJ]
- **Mistral formats on vLLM.** `serve_vllm.sh` always passes `--tokenizer-mode mistral --config-format mistral --load-format mistral`; another model through this script needs those flags removed. [serve_vllm.sh]

## Side Effects

- `serve_llamacpp.sh` without `GGUF` downloads the model and its vision projector from Hugging Face on its first run.
- `serve_vllm.sh` with a repository name downloads the weights unless `HF_HUB_OFFLINE=1` and a local folder are given.
- `COA_LLM_LOG` makes COA-Net append every prompt and answer to that file, without image data. [coa_net/llm.py:OpenAICompatibleClient._log]
- Both scripts open a listening port (`PORT`, default 8000) on 127.0.0.1 and hold GPU memory while running (`GPU_UTIL`, default 0.90 of the GPU, for vLLM; `NGL` layers for llama.cpp).
- `coa-net llm-check --out FILE` writes the check report as JSON, creating the parent folder. [coa_net/cli.py:cmd_llm_check]

## Verification

- Run: `coa-net llm-check` against the running server
- Run: `pytest tests/test_local_llm.py` (the client against a stand-in local server, without a model)
- Run: `pytest` (the whole suite; `testpaths = ["tests"]` in `pyproject.toml`)

Sources: the model card of `mistralai/Mistral-Small-3.2-24B-Instruct-2506` (memory, vLLM version, temperature, licence); the GGUF repositories of unsloth and gabriellarson (file sizes); the llama.cpp server README (`--mmproj`, `-hf`, `--jinja`, `/health`); the vLLM structured-outputs page (`response_format` with `json_schema`); the Ollama OpenAI-compatibility page (port 11434, base64 images); the Ollama library page of `mistral-small3.2` (vision, tools); the `Qwen/Qwen3-8B-GGUF` page (file sizes, licence, sampling advice).

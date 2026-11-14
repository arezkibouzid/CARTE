"""Starts the local language model on what the machine has, and tells the app where it is.

    python deploy/llm_launch.py              # GPU found: vLLM; else CPU (llama.cpp if installed, else transformers)
    python deploy/llm_launch.py --dry-run    # only says what it would start
    python deploy/llm_launch.py --device cpu # force the CPU path

The same model everywhere, Qwen3-VL-8B-Instruct (the best measured, experiments/README.md):
- GPU: vLLM, bfloat16, 131,072-token window;
- CPU, llama.cpp (``llama-server`` on the PATH, in ``$AMIAD_LLAMA_SERVER`` or ``~/ext/llama.cpp/build/bin``): the
  official 4-bit build (Qwen/Qwen3-VL-8B-Instruct-GGUF, Q4_K_M, 5.0 GB) with its image encoder (mmproj F16), 32,768
  tokens, JSON schema enforced by llama.cpp's grammar;
- CPU without llama.cpp: deploy/cpu_llm_server.py (transformers, bfloat16; same weights and JSON grammar, much slower:
  a written order in 22 s and a chat answer in about 9 minutes on 32 cores).
``AMIAD_CPU_MODEL=Qwen/Qwen3-VL-4B-Instruct`` uses the smaller model on the transformers path. Missing weights are
downloaded from Hugging Face into ``$AMIAD_MODELS`` (``~/extraction/models``). Writes ``outputs/orders/llm_endpoints.json``
(removed on exit), the file the app reads. Slurm: deploy/serve_llm.sbatch (GPU), deploy/serve_llm_cpu.sbatch (CPU, runs
this script).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODEL = "Qwen/Qwen3-VL-8B-Instruct"
CPU_MODEL = os.environ.get("AMIAD_CPU_MODEL", MODEL)  # transformers path only
GGUF_REPO = "Qwen/Qwen3-VL-8B-Instruct-GGUF"
GGUF_FILES = ("Qwen3VL-8B-Instruct-Q4_K_M.gguf", "mmproj-Qwen3VL-8B-Instruct-F16.gguf")


def has_gpu() -> bool:
    if os.environ.get("CUDA_VISIBLE_DEVICES", None) == "":
        return False
    smi = shutil.which("nvidia-smi")
    if not smi:
        return False
    try:
        return "GPU" in subprocess.run([smi, "-L"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return False


def llama_server() -> str | None:
    for c in (os.environ.get("AMIAD_LLAMA_SERVER"), shutil.which("llama-server"),
              str(Path.home() / "ext/llama.cpp/build/bin/llama-server")):
        if c and Path(c).exists():
            return c
    return None


def weights(repo: str, models: Path) -> Path:
    d = models / repo.split("/")[1]
    if not (d / "config.json").exists():
        from huggingface_hub import snapshot_download

        print(f"downloading {repo} from Hugging Face to {d} ...", flush=True)
        snapshot_download(repo, local_dir=str(d))
    return d


def gguf(models: Path) -> tuple[Path, Path]:
    d = models / GGUF_REPO.split("/")[1]
    for f in GGUF_FILES:
        if not (d / f).exists():
            from huggingface_hub import hf_hub_download

            print(f"downloading {GGUF_REPO}/{f} from Hugging Face ...", flush=True)
            hf_hub_download(GGUF_REPO, f, local_dir=str(d))
    return d / GGUF_FILES[0], d / GGUF_FILES[1]


def free_port() -> int:
    s = socket.socket()
    s.bind(("0.0.0.0", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def plan(a) -> dict:
    gpu = a.device == "gpu" or (a.device == "auto" and has_gpu())
    if gpu:
        return {"device": "gpu", "backend": "vllm", "model": MODEL}
    ls = llama_server() if a.cpu_backend in ("auto", "llamacpp") else None
    if a.cpu_backend == "llamacpp" and not ls:
        sys.exit("llama-server not found: build llama.cpp (deploy/README.md) or set AMIAD_LLAMA_SERVER")
    if ls:
        return {"device": "cpu", "backend": "llama.cpp", "model": MODEL, "binary": ls, "quant": "Q4_K_M"}
    return {"device": "cpu", "backend": "transformers", "model": CPU_MODEL}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=("auto", "gpu", "cpu"), default="auto")
    ap.add_argument("--cpu-backend", choices=("auto", "llamacpp", "transformers"), default=os.environ.get("AMIAD_CPU_BACKEND", "auto"))
    ap.add_argument("--models", type=Path, default=Path(os.environ.get("AMIAD_MODELS", Path.home() / "extraction" / "models")))
    ap.add_argument("--vllm", default=os.environ.get("AMIAD_VLLM", str(Path.home() / "extraction/.venv-vllm/bin/vllm")))
    ap.add_argument("--python", default=os.environ.get("AMIAD_VLLM_PY", str(Path.home() / "extraction/.venv-vllm/bin/python")),
                    help="transformers CPU server: a python with torch, transformers, xgrammar, fastapi, uvicorn")
    ap.add_argument("--threads", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK") or os.cpu_count()))
    ap.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to serve other machines (the endpoint names this host)")
    ap.add_argument("--endpoint-file", type=Path, default=REPO / "outputs/orders/llm_endpoints.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    P = plan(a)
    print(f"{P['device'].upper()}: {P['model']} with {P['backend']}" + (f" ({P['quant']})" if "quant" in P else ""), flush=True)
    if a.dry_run:
        return
    port = free_port()
    if P["backend"] == "vllm":
        d = weights(MODEL, a.models)
        vllm = a.vllm if Path(a.vllm).exists() else (shutil.which("vllm") or a.vllm)
        cmd = [vllm, "serve", str(d), "--served-model-name", MODEL, "--host", a.host, "--port", str(port), "--dtype", "bfloat16",
               "--max-model-len", "131072", "--max-num-seqs", "4", "--gpu-memory-utilization", os.environ.get("AMIAD_GPU_UTIL", "0.55"),
               "--limit-mm-per-prompt", '{"image":8}', "--mm-processor-kwargs", '{"max_pixels":2508800}',
               "--structured-outputs-config", '{"backend":"xgrammar","disable_any_whitespace":true,"disable_fallback":true}',
               "--generation-config", "vllm"]
    elif P["backend"] == "llama.cpp":
        m, mm = gguf(a.models)
        cmd = [P["binary"], "-m", str(m), "--mmproj", str(mm), "--jinja", "-c", "32768", "-t", str(a.threads), "-ngl", "0",
               "--temp", "0", "--alias", MODEL, "--host", a.host, "--port", str(port)]
    else:
        d = weights(CPU_MODEL, a.models)
        cmd = [a.python, "-u", str(REPO / "deploy/cpu_llm_server.py"), "--model", str(d), "--name", CPU_MODEL, "--host", a.host,
               "--port", str(port), "--threads", str(a.threads), "--max-len", "32768"]
    log = REPO / f"outputs/orders/llm-{P['device']}-{P['backend']}-{os.environ.get('SLURM_JOB_ID', 'local')}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **({} if P["device"] == "gpu" else {"CUDA_VISIBLE_DEVICES": ""}))
    p = subprocess.Popen(cmd, stdout=open(log, "w"), stderr=subprocess.STDOUT, env=env)

    def stop(*_):
        p.terminate()
        a.endpoint_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for _ in range(900):
        if p.poll() is not None:
            a.endpoint_file.unlink(missing_ok=True)
            sys.exit(f"the model server stopped: see {log}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            pass
        time.sleep(2)
    else:
        stop()
    host = "127.0.0.1" if a.host == "127.0.0.1" else socket.getfqdn()
    ep = {"base_url": f"http://{host}:{port}/v1", "model": MODEL if P["backend"] != "transformers" else CPU_MODEL}
    a.endpoint_file.write_text(json.dumps({"job": os.environ.get("SLURM_JOB_ID", f"local-{os.getpid()}"), "device": P["device"],
                                           "backend": P["backend"], "extraction": ep, "explanation": ep}), encoding="utf-8")
    print(f"ready: {ep['model']} at {ep['base_url']} ({P['backend']}, log {log})", flush=True)
    try:
        p.wait()
    finally:
        a.endpoint_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

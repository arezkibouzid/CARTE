"""Starts the local language model the machine can run, and tells the app where it is.

    python deploy/llm_launch.py              # GPU found: Qwen3-VL-8B with vLLM (131,072 tokens); else the same model on CPU
    python deploy/llm_launch.py --dry-run    # only says what it would start
    python deploy/llm_launch.py --device cpu # force the CPU model

The same model either way, Qwen3-VL-8B (the best measured, experiments/README.md): vLLM on a GPU, deploy/cpu_llm_server.py
on CPU only (same weights, JSON grammar and image input; slower: a written order in about 22 s on 32 cores).
``AMIAD_CPU_MODEL=Qwen/Qwen3-VL-4B-Instruct`` picks the smaller model on CPU (14 s, but less reliable). Missing weights are downloaded from Hugging Face into ``$AMIAD_MODELS`` (``~/extraction/models``). Writes
``outputs/orders/llm_endpoints.json`` (removed on exit), the file the app reads. On the cluster, the Slurm scripts
deploy/serve_llm.sbatch (GPU) and deploy/serve_llm_cpu.sbatch (CPU) do the same.
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
GPU_MODEL = "Qwen/Qwen3-VL-8B-Instruct"
CPU_MODEL = os.environ.get("AMIAD_CPU_MODEL", "Qwen/Qwen3-VL-8B-Instruct")


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


def weights(repo: str, models: Path) -> Path:
    d = models / repo.split("/")[1]
    if not (d / "config.json").exists():
        from huggingface_hub import snapshot_download

        print(f"downloading {repo} from Hugging Face to {d} ...", flush=True)
        snapshot_download(repo, local_dir=str(d))
    return d


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", choices=("auto", "gpu", "cpu"), default="auto")
    ap.add_argument("--models", type=Path, default=Path(os.environ.get("AMIAD_MODELS", Path.home() / "extraction" / "models")))
    ap.add_argument("--vllm", default=os.environ.get("AMIAD_VLLM", str(Path.home() / "extraction/.venv-vllm/bin/vllm")))
    ap.add_argument("--python", default=os.environ.get("AMIAD_VLLM_PY", str(Path.home() / "extraction/.venv-vllm/bin/python")),
                    help="CPU server: a python with torch, transformers, xgrammar, fastapi, uvicorn")
    ap.add_argument("--threads", type=int, default=os.cpu_count())
    ap.add_argument("--endpoint-file", type=Path, default=REPO / "outputs/orders/llm_endpoints.json")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    gpu = a.device == "gpu" or (a.device == "auto" and has_gpu())
    repo = GPU_MODEL if gpu else CPU_MODEL
    print(f"{'GPU' if gpu else 'CPU only'}: {repo}", flush=True)
    if a.dry_run:
        return
    d = weights(repo, a.models)
    port = free_port()
    if gpu:
        vllm = a.vllm if Path(a.vllm).exists() else (shutil.which("vllm") or a.vllm)
        cmd = [vllm, "serve", str(d), "--served-model-name", repo, "--host", "127.0.0.1", "--port", str(port), "--dtype", "bfloat16",
               "--max-model-len", "131072", "--max-num-seqs", "4", "--gpu-memory-utilization", os.environ.get("AMIAD_GPU_UTIL", "0.55"),
               "--limit-mm-per-prompt", '{"image":8}', "--mm-processor-kwargs", '{"max_pixels":2508800}',
               "--structured-outputs-config", '{"backend":"xgrammar","disable_any_whitespace":true,"disable_fallback":true}',
               "--generation-config", "vllm"]
    else:
        cmd = [a.python, "-u", str(REPO / "deploy/cpu_llm_server.py"), "--model", str(d), "--name", repo, "--port", str(port),
               "--threads", str(a.threads), "--max-len", "32768"]
    log = REPO / f"outputs/orders/llm-{'gpu' if gpu else 'cpu'}-local.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, **({} if gpu else {"CUDA_VISIBLE_DEVICES": ""}))
    p = subprocess.Popen(cmd, stdout=open(log, "w"), stderr=subprocess.STDOUT, env=env)

    def stop(*_):
        p.terminate()
        a.endpoint_file.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for _ in range(600):
        if p.poll() is not None:
            sys.exit(f"the model server stopped: see {log}")
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2)
            break
        except OSError:
            time.sleep(2)
    else:
        stop()
    ep = {"base_url": f"http://127.0.0.1:{port}/v1", "model": repo}
    a.endpoint_file.write_text(json.dumps({"job": f"local-{os.getpid()}", "device": "gpu" if gpu else "cpu", "extraction": ep, "explanation": ep}),
                               encoding="utf-8")
    print(f"ready: {repo} at {ep['base_url']} (log {log})", flush=True)
    try:
        p.wait()
    finally:
        a.endpoint_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

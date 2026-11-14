"""The language model on CPU only: an OpenAI-compatible server for the parts of the API CARTE uses, no GPU.

Same weights as on the GPU (Qwen3-VL-8B-Instruct, bfloat16), same greedy decoding, same JSON grammar per request
(xgrammar, as vLLM uses), images accepted (order pages). Requests are served one at a time. Endpoints: GET /health,
GET /v1/models, POST /v1/chat/completions (``messages``, ``max_tokens``, ``response_format`` json_schema).

    python deploy/cpu_llm_server.py --model ~/extraction/models/Qwen3-VL-8B-Instruct --port 8010 --threads 32
    (or: sbatch deploy/serve_llm_cpu.sbatch, which writes the endpoint file the app reads)

Needs torch, transformers >= 4.57, xgrammar, fastapi, uvicorn (the vLLM environment has them all). Slower than the GPU:
see ``docs/technical_overview.md`` for measured times; set ``AMIAD_CHAT_BUDGET_TOKENS`` lower on the app side to keep
answers short.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import threading
import time
import uuid

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

import torch  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from PIL import Image  # noqa: E402

app = FastAPI()
S: dict = {}
LOCK = threading.Lock()


def load(model_dir: str, threads: int, max_pixels: int):
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    import xgrammar as xgr

    torch.set_num_threads(threads)
    t0 = time.time()
    proc = AutoProcessor.from_pretrained(model_dir)
    proc.image_processor.size = {"longest_edge": max_pixels, "shortest_edge": 65536}
    model = Qwen3VLForConditionalGeneration.from_pretrained(model_dir, dtype=torch.bfloat16).eval()
    info = xgr.TokenizerInfo.from_huggingface(proc.tokenizer, vocab_size=model.config.text_config.vocab_size)
    S.update(proc=proc, model=model, compiler=xgr.GrammarCompiler(info, max_threads=4), xgr=xgr)
    print(f"loaded {model_dir} on CPU ({threads} threads) in {time.time() - t0:.0f} s", flush=True)


def to_qwen(messages: list[dict]) -> list[dict]:
    """OpenAI messages (text, image_url data URLs) to the processor's chat format (text, PIL images)."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": [{"type": "text", "text": c}]})
            continue
        parts = []
        for p in c or []:
            if p.get("type") == "text":
                parts.append({"type": "text", "text": p["text"]})
            elif p.get("type") == "image_url":
                url = p["image_url"]["url"] if isinstance(p["image_url"], dict) else p["image_url"]
                if not url.startswith("data:"):
                    raise HTTPException(400, "only data: image URLs (the server reads nothing from the network)")
                parts.append({"type": "image", "image": Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1]))).convert("RGB")})
        out.append({"role": m["role"], "content": parts})
    return out


@app.get("/health")
def health():
    return {"ok": "model" in S}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": S["name"], "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
def chat(req: dict):
    proc, model = S["proc"], S["model"]
    msgs = to_qwen(req["messages"])
    inputs = proc.apply_chat_template(msgs, tokenize=True, add_generation_prompt=True, return_dict=True, return_tensors="pt")
    n_in = int(inputs["input_ids"].shape[1])
    if n_in > S["max_len"]:
        raise HTTPException(400, f"prompt of {n_in} tokens exceeds the window ({S['max_len']})")
    kw = {"max_new_tokens": int(req.get("max_tokens") or 1024), "do_sample": False}
    rf = req.get("response_format") or {}
    if rf.get("type") == "json_schema":
        schema = rf["json_schema"]["schema"]
        cg = S["compiler"].compile_json_schema(json.dumps(schema), any_whitespace=False)
        kw["logits_processor"] = [S["xgr"].contrib.hf.LogitsProcessor(cg)]
    t0 = time.time()
    with LOCK, torch.inference_mode():
        out = model.generate(**inputs, **kw)
    gen = out[0, n_in:]
    text = proc.decode(gen, skip_special_tokens=True)
    dt = time.time() - t0
    print(f"prompt {n_in} tok, answer {len(gen)} tok, {dt:.1f} s", flush=True)
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "created": int(time.time()), "model": S["name"],
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": n_in, "completion_tokens": int(len(gen)), "total_tokens": n_in + int(len(gen))}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.path.expanduser("~/extraction/models/Qwen3-VL-8B-Instruct"))
    ap.add_argument("--name", default="Qwen/Qwen3-VL-8B-Instruct")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8010)
    ap.add_argument("--threads", type=int, default=os.cpu_count())
    ap.add_argument("--max-len", type=int, default=32768)
    ap.add_argument("--max-pixels", type=int, default=1003520)  # fewer image tokens than on the GPU (2,508,800): CPU time
    a = ap.parse_args()
    S.update(name=a.name, max_len=a.max_len)
    load(a.model, a.threads, a.max_pixels)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()

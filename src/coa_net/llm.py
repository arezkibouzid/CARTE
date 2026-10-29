"""Small-LLM access, hardware agnostic (reused from D2C-Route).

COA-Net talks to any OpenAI-compatible chat endpoint, which covers the
usual local runtimes for <= tens-of-billions-parameter open models: vLLM
(``vllm serve Qwen/Qwen3-14B``), llama.cpp ``llama-server``, Ollama, LM Studio
or TGI. Outputs are requested as JSON, constrained by a JSON schema when the
server supports it, and always validated by the caller.

Every language-model role (L1 to L8 in the methodology) has a deterministic
offline fallback, so the pipeline runs without any model; set
``COA_LLM_BASE_URL`` and ``COA_LLM_MODEL`` to enable one. Token usage is
recorded per stage for the frugality metrics.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMClient(Protocol):
    usage: "Usage"

    def complete_json(self, system: str, user: str, schema: dict, stage: str = "") -> dict: ...


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    by_stage: dict[str, dict[str, float]] = field(default_factory=dict)

    def record(self, stage: str, inp: int, out: int, seconds: float) -> None:
        self.calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        self.seconds += seconds
        s = self.by_stage.setdefault(stage or "other", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0})
        s["calls"] += 1
        s["input_tokens"] += inp
        s["output_tokens"] += out
        s["seconds"] += seconds


def _extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # reasoning models
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, flags=re.S)
        if not m:
            raise
        return json.loads(m.group(0))


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout: float = 120.0,
        use_json_schema: bool = True,
        max_retries: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or "not-needed"
        self.temperature = temperature
        self.timeout = timeout
        self.use_json_schema = use_json_schema
        self.max_retries = max_retries
        self.usage = Usage()

    def _post(self, payload: dict) -> dict:
        import requests

        r = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json()

    def complete_json(self, system: str, user: str, schema: dict, stage: str = "") -> dict:
        messages = [
            {"role": "system", "content": system + "\nReply with a single JSON object and nothing else."},
            {"role": "user", "content": user + "\n\nJSON schema of the answer:\n" + json.dumps(schema)},
        ]
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if self.use_json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        last: Exception | None = None
        for _ in range(self.max_retries + 1):
            t0 = time.time()
            try:
                data = self._post(payload)
            except Exception as exc:  # noqa: BLE001
                if self.use_json_schema and "response_format" in payload:
                    payload.pop("response_format")  # server without structured output: plain JSON prompt
                    last = exc
                    continue
                raise
            u = data.get("usage") or {}
            self.usage.record(stage, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), time.time() - t0)
            try:
                return _extract_json(data["choices"][0]["message"]["content"])
            except (json.JSONDecodeError, KeyError, IndexError) as exc:
                last = exc
        raise RuntimeError(f"LLM did not return valid JSON: {last}")


class ScriptedLLM:
    """Deterministic stand-in for tests and demos: ``responder(stage, user) -> dict``."""

    def __init__(self, responder: Callable[[str, str], dict]):
        self.responder = responder
        self.usage = Usage()

    def complete_json(self, system: str, user: str, schema: dict, stage: str = "") -> dict:
        out = self.responder(stage, user)
        self.usage.record(stage, (len(system) + len(user)) // 4, len(json.dumps(out)) // 4, 0.0)
        return out


def from_env() -> OpenAICompatibleClient | None:
    """Client from ``COA_LLM_BASE_URL`` / ``COA_LLM_MODEL`` / ``COA_LLM_API_KEY``, or None."""
    base = os.environ.get("COA_LLM_BASE_URL")
    model = os.environ.get("COA_LLM_MODEL")
    if not base or not model:
        return None
    return OpenAICompatibleClient(
        base,
        model,
        os.environ.get("COA_LLM_API_KEY"),
        use_json_schema=os.environ.get("COA_LLM_JSON_SCHEMA", "1") != "0",
    )


_NUM = re.compile(r"(?<![\w.])(\d{1,2}:\d{2}|\d+(?:[.,]\d+)?)(?![\w])")
_IDENT = re.compile(r"\b([A-Z]{2,}(?:-\d+)?|[A-Z]+-\d+|[A-Z]\d+)\b")


def _norm_num(x: str) -> str:
    x = x.replace(",", ".")
    if ":" in x:
        return x
    try:
        v = float(x)
    except ValueError:
        return x
    return f"{v:g}"


def unsupported(text: str, facts: str, extra_words: set[str] | None = None) -> list[str]:
    """Numbers, times and identifiers of ``text`` that do not appear in ``facts`` (generated-prose guardrail)."""
    allowed_nums = {_norm_num(m) for m in _NUM.findall(facts)}
    allowed_ids = set(_IDENT.findall(facts)) | (extra_words or set())
    bad = []
    for m in _NUM.findall(text):
        if _norm_num(m) not in allowed_nums and m not in ("1", "2", "3", "4", "5"):  # paragraph numbers
            bad.append(m)
    for m in _IDENT.findall(text):
        if m not in allowed_ids and m not in ("COA", "MSR", "ASR", "FRAGO", "TCP", "SPOF", "MLC", "SP", "RP", "OK", "NATO"):
            bad.append(m)
    return sorted(set(bad))


def call_validated(llm, system: str, user: str, schema: dict, stage: str, validate, retries: int = 1):
    """Ask for JSON, validate it with ``validate(out) -> (value, problems)``; one retry, then None.

    Every role uses this guard: schema-constrained output, a code check, one
    retry with the problems listed, then the deterministic fallback."""
    if llm is None:
        return None, ["no language model configured"]
    problems: list[str] = []
    prompt = user
    for _ in range(retries + 1):
        try:
            out = llm.complete_json(system, prompt, schema, stage=stage)
        except Exception as exc:  # noqa: BLE001
            problems = [f"call failed: {exc}"]
            continue
        value, problems = validate(out)
        if not problems:
            return value, []
        prompt = user + "\n\nYour previous answer was rejected: " + "; ".join(problems[:5]) + ". Answer again."
    return None, problems

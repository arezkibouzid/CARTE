"""Access to a locally deployed open model, hardware agnostic.

The model runs on a server the team controls: vLLM on the organisers' H100,
or llama.cpp, Ollama or LM Studio on a smaller machine (``deploy/README.md``).
COA-Net talks to it through the OpenAI-compatible chat endpoint those servers
share. Outputs are requested as JSON, constrained by a JSON schema when the
server supports it; ``call_validated`` checks their shape against that schema
before the caller's own validation, since a server that refuses the schema
gets a plain JSON prompt.

Local by default: without ``COA_LLM_BASE_URL`` the client looks for a server
on this machine (``LOCAL_DEFAULT_URL``, vLLM's port), takes the model it serves
unless ``COA_LLM_MODEL`` names one, and refuses an endpoint outside this
machine and its private network unless ``COA_LLM_ALLOW_REMOTE=1``, so that
doctrine and orders are not sent to a hosted API by mistake. A server that does
not answer fails fast: the connection times out after ``COA_LLM_CONNECT_TIMEOUT``
seconds (default 5), a call after ``COA_LLM_TIMEOUT`` (default 120), and after
the first failure the client stops calling it for the rest of the run, so that
every later role uses its fallback at once. ``COA_LLM_LOG`` names a JSON-lines
file where prompts and answers are kept on this machine (images left out).

Every language-model role (L1 to L9 in the methodology) has a deterministic
offline fallback, so the pipeline runs without any model. The sketch reader
(L9) sends the numbered sketch as an image, so it needs a vision-language
model behind the same endpoint (Mistral Small 3.2 takes images). Token usage
is recorded per stage for the frugality metrics.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

LOCAL_DEFAULT_URL = "http://127.0.0.1:8000/v1"  # vLLM's default port; llama.cpp serves on 8080 and Ollama on 11434 by default


class LLMUnavailable(RuntimeError):
    """The model server cannot be reached or did not answer in time; the caller uses its fallback."""


class LLMConfigError(ValueError):
    """The model configuration is refused, for example a remote endpoint without ``COA_LLM_ALLOW_REMOTE=1``."""


def is_local_url(url: str, resolve: bool = True) -> bool:
    """True when the endpoint is this machine or a private network: an address that is not globally routable (loopback,
    RFC 1918, link-local, shared address space), ``localhost`` or a ``.local`` name. Another host name is resolved and is
    local only if every address it resolves to is."""
    import ipaddress
    import socket
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True

    def private(a: str) -> bool:
        return not ipaddress.ip_address(a.split("%")[0]).is_global

    try:
        return private(host)
    except ValueError:
        pass
    if not resolve:
        return False
    try:
        addrs = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(addrs) and all(private(a) for a in addrs)


def _without_images(messages: list[dict]) -> list[dict]:
    """Messages for the local log: image data replaced by its size."""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [{"type": "image_url", "image_url": f"<image, {len(p['image_url'].get('url', ''))} characters>"}
                 if p.get("type") == "image_url" else p for p in c]
        out.append({**m, "content": c})
    return out


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
        connect_timeout: float = 5.0,
        log_path: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or "not-needed"
        self.temperature = temperature
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.use_json_schema = use_json_schema
        self.max_retries = max_retries
        self.log_path = log_path
        self.usage = Usage()
        self.down: str | None = None  # why the server stopped answering; then every later call fails at once
        self.schema_rejected = 0  # calls where the server refused schema-constrained output (plain JSON prompt used)

    @property
    def local(self) -> bool:
        return is_local_url(self.base_url)

    def describe(self) -> dict:
        """Where the model runs, for reports and metrics."""
        return {"base_url": self.base_url, "model": self.model, "local": self.local, "json_schema": self.use_json_schema,
                "schema_rejected": self.schema_rejected, "down": self.down}

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    def models(self) -> list[str]:
        """Models the server serves (``GET /models``); the startup health check."""
        import requests

        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=(self.connect_timeout, 10.0))
            r.raise_for_status()
            data = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise LLMUnavailable(f"no model server answers at {self.base_url} ({type(exc).__name__}: {exc})") from exc
        return [m["id"] for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]

    def _post(self, payload: dict) -> dict:
        import requests

        if self.down:
            raise LLMUnavailable(f"model server not called: it stopped answering earlier in this run ({self.down})")
        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=(self.connect_timeout, self.timeout),
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            self.down = f"{type(exc).__name__} at {time.strftime('%H:%M:%S')}"
            raise LLMUnavailable(f"model server at {self.base_url} did not answer ({type(exc).__name__}); the role uses its fallback") from exc
        r.raise_for_status()
        return r.json()

    def _log(self, stage: str, messages: list[dict], data: dict | None, seconds: float, error: str = "") -> None:
        if not self.log_path:
            return
        rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%S"), "stage": stage, "model": self.model, "seconds": round(seconds, 3),
               "messages": _without_images(messages), "error": error}
        if data is not None:
            rec["answer"] = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
            rec["usage"] = data.get("usage")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.log_path)), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def complete_json(self, system: str, user: str, schema: dict, stage: str = "") -> dict:
        messages = [
            {"role": "system", "content": system + "\nReply with a single JSON object and nothing else."},
            {"role": "user", "content": user + "\n\nJSON schema of the answer:\n" + json.dumps(schema)},
        ]
        return self._complete(messages, schema, stage)

    def complete_json_images(self, system: str, user: str, schema: dict, images: list[str], stage: str = "") -> dict:
        """Same as ``complete_json`` with images (data URLs or http URLs) after the text, for vision-language models (L9)."""
        content: list[dict] = [{"type": "text", "text": user + "\n\nJSON schema of the answer:\n" + json.dumps(schema)}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
        messages = [{"role": "system", "content": system + "\nReply with a single JSON object and nothing else."},
                    {"role": "user", "content": content}]
        return self._complete(messages, schema, stage)

    def _complete(self, messages: list[dict], schema: dict, stage: str) -> dict:
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
        if self.use_json_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema}}
        last: Exception | None = None
        dropped = False
        for _ in range(self.max_retries + 1):
            t0 = time.time()
            try:
                data = self._post(payload)
            except LLMUnavailable as exc:
                self._log(stage, messages, None, time.time() - t0, str(exc))
                raise  # no retry: the server is down or too slow, and the caller falls back
            except Exception as exc:  # noqa: BLE001
                self._log(stage, messages, None, time.time() - t0, f"{type(exc).__name__}: {exc}")
                if "response_format" in payload:
                    payload.pop("response_format")  # server without structured output: plain JSON prompt
                    dropped = True
                    last = exc
                    continue
                raise
            if dropped:  # the server answered only without the schema constraint
                self.schema_rejected += 1
                dropped = False
            u = data.get("usage") or {}
            self.usage.record(stage, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), time.time() - t0)
            self._log(stage, messages, data, time.time() - t0)
            try:
                return _extract_json(data["choices"][0]["message"]["content"])
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
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

    def complete_json_images(self, system: str, user: str, schema: dict, images: list[str], stage: str = "") -> dict:
        self.images_seen = getattr(self, "images_seen", 0) + len(images)
        return self.complete_json(system, user, schema, stage)


def from_env(check: bool = True) -> OpenAICompatibleClient:
    """Client for the locally deployed model, from the environment.

    ``COA_LLM_BASE_URL`` (default ``LOCAL_DEFAULT_URL``), ``COA_LLM_MODEL`` (default: the first model the server serves),
    ``COA_LLM_API_KEY``, ``COA_LLM_JSON_SCHEMA`` (``0`` for a server without schema-constrained output),
    ``COA_LLM_TIMEOUT`` and ``COA_LLM_CONNECT_TIMEOUT`` (seconds), ``COA_LLM_LOG`` (JSON-lines file of prompts and answers).
    An endpoint outside this machine and its private network raises ``LLMConfigError`` unless ``COA_LLM_ALLOW_REMOTE=1``.
    With ``check``, the server is asked for its models first, and ``LLMUnavailable`` is raised if it does not answer."""
    base = os.environ.get("COA_LLM_BASE_URL") or LOCAL_DEFAULT_URL
    if not is_local_url(base) and os.environ.get("COA_LLM_ALLOW_REMOTE", "0") != "1":
        raise LLMConfigError(f"{base} is not on this machine or a private network: COA-Net sends doctrine and orders only to a locally "
                             "deployed model. Serve the model locally (deploy/README.md), reach a remote server through an SSH "
                             "tunnel to 127.0.0.1, or set COA_LLM_ALLOW_REMOTE=1 to accept this endpoint")
    llm = OpenAICompatibleClient(
        base,
        os.environ.get("COA_LLM_MODEL", ""),
        os.environ.get("COA_LLM_API_KEY"),
        use_json_schema=os.environ.get("COA_LLM_JSON_SCHEMA", "1") != "0",
        timeout=float(os.environ.get("COA_LLM_TIMEOUT", "120")),
        connect_timeout=float(os.environ.get("COA_LLM_CONNECT_TIMEOUT", "5")),
        log_path=os.environ.get("COA_LLM_LOG") or None,
    )
    if check or not llm.model:
        served = llm.models()
        if not llm.model:
            if not served:
                raise LLMUnavailable(f"the server at {base} lists no model")
            llm.model = served[0]
    return llm


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


_JSON_TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


def schema_problems(out, schema: dict) -> list[str]:
    """Top-level shape check: the answer's type, its required fields and the types of the fields it gives.

    A server that refuses the schema gets a plain JSON prompt (``OpenAICompatibleClient._complete``), so its answer is
    not guaranteed to follow the schema; this check catches an empty or partial record before the role's own check."""
    want = _JSON_TYPES.get(schema.get("type", ""))
    if want is None:
        return []
    if not isinstance(out, want) or (want is int and isinstance(out, bool)):
        return [f"the answer is not a JSON {schema['type']}"]
    if not isinstance(out, dict):
        return []
    problems = [f"field {k!r} is missing" for k in schema.get("required", []) if k not in out]
    for k, sub in (schema.get("properties") or {}).items():
        t = _JSON_TYPES.get(sub.get("type", "")) if isinstance(sub, dict) else None
        if k in out and t is not None and (not isinstance(out[k], t) or (t is int and isinstance(out[k], bool))):
            problems.append(f"field {k!r} is not a {sub['type']}")
    return problems


def call_validated(llm, system: str, user: str, schema: dict, stage: str, validate, retries: int = 1):
    """Ask for JSON, validate it with ``validate(out) -> (value, problems)``; one retry, then None.

    Every role uses this guard: schema-constrained output, a shape check against the schema, the role's own check, one
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
        problems = schema_problems(out, schema)
        if problems:
            prompt = user + "\n\nYour previous answer was rejected: " + "; ".join(problems[:5]) + ". Answer again."
            continue
        value, problems = validate(out)
        if not problems:
            return value, []
        prompt = user + "\n\nYour previous answer was rejected: " + "; ".join(problems[:5]) + ". Answer again."
    return None, problems

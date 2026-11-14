"""Model clients. The pipeline never needs a model: every role has a pattern fallback.

OpenAICompatClient talks to a local server (vLLM, llama.cpp, Ollama) through the OpenAI-style
/chat/completions endpoint. An address outside this machine and its private network is refused unless
allow_remote=True. ScriptedLLM is for tests.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
from urllib.parse import urlparse


class LLMUnavailable(Exception):
    pass


class LLMConfigError(Exception):
    pass


def is_local_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    if host in {"localhost"} or host.endswith((".localhost", ".local")):
        return True
    try:
        infos = {i[4][0] for i in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    return bool(infos) and all(
        ipaddress.ip_address(a).is_private or ipaddress.ip_address(a).is_loopback for a in infos)


class ScriptedLLM:
    """Returns canned answers: a dict, a list of dicts (one per call), or a function(system, user)."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def complete_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        self.calls.append((system, user))
        if callable(self.responses):
            return self.responses(system, user)
        if isinstance(self.responses, list):
            return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return self.responses


class OpenAICompatClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, api_key: str | None = None,
                 timeout: float = 120.0, connect_timeout: float = 5.0, allow_remote: bool = False,
                 use_schema: bool = True, log_path: str | None = None):
        self.base_url = (base_url or os.environ.get("DOCRAG_LLM_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
        if not allow_remote and not is_local_url(self.base_url):
            raise LLMConfigError(
                f"{self.base_url} is not on this machine or a private network. "
                "Use an SSH tunnel to 127.0.0.1, or pass allow_remote=True.")
        self.model = model or os.environ.get("DOCRAG_LLM_MODEL")
        self.api_key = api_key or os.environ.get("DOCRAG_LLM_API_KEY")
        self.timeout, self.connect_timeout = timeout, connect_timeout
        self.use_schema, self.log_path = use_schema, log_path
        self.down: str | None = None

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def models(self) -> list[str]:
        import requests

        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=self.connect_timeout)
            r.raise_for_status()
        except Exception as e:
            self.down = str(e)
            raise LLMUnavailable(str(e)) from e
        return [m["id"] for m in r.json().get("data", [])]

    def complete_json(self, system: str, user: str, schema: dict | None = None) -> dict:
        import requests

        if self.down:
            raise LLMUnavailable(self.down)
        if not self.model:
            self.model = (self.models() or [None])[0]
        body = {"model": self.model, "temperature": 0,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if schema and self.use_schema:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "schema": schema, "strict": False}}
        try:
            r = requests.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                              data=json.dumps(body), timeout=(self.connect_timeout, self.timeout))
            if r.status_code >= 400 and "response_format" in body:  # server without schema support
                body.pop("response_format")
                r = requests.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                                  data=json.dumps(body), timeout=(self.connect_timeout, self.timeout))
            r.raise_for_status()
        except (requests.ConnectionError, requests.Timeout) as e:
            self.down = str(e)  # fail fast for the rest of the run
            raise LLMUnavailable(str(e)) from e
        content = r.json()["choices"][0]["message"]["content"]
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"system": system[:200], "user": user, "answer": content}) + "\n")
        return parse_json(content)


def parse_json(content: str) -> dict:
    s = content.strip()
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.S)
        if m:
            return json.loads(m.group(0))
        raise

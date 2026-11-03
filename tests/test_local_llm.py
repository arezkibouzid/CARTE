"""The locally deployed model: endpoint policy, model discovery, fast failure, schema fallback, images, local log, llm-check.

A stand-in OpenAI-compatible server on 127.0.0.1 plays the local model server (vLLM, llama.cpp, Ollama), so the HTTP path of
the client is tested without a model."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from coa_net.llm import (LOCAL_DEFAULT_URL, LLMConfigError, LLMUnavailable, OpenAICompatibleClient, ScriptedLLM, call_validated,
                         from_env, is_local_url, schema_problems)


def _default_answer(text: str, has_image: bool) -> dict:
    if "17 + 25" in text:
        return {"sum": 42}
    if has_image:
        return {"colour": "red"}
    if "120 words" in text:
        return {"text": " ".join(["convoy"] * 120)}
    if '{"ok": true}' in text:
        return {"ok": True}
    return {}


class StubServer:
    """Minimal OpenAI-compatible server: GET /v1/models and POST /v1/chat/completions, with switchable failures."""

    def __init__(self, models=("stub-model",), reject_schema=False, reject_images=False, delay=0.0):
        self.models = list(models)
        self.reject_schema = reject_schema
        self.reject_images = reject_images
        self.delay = delay
        self.requests: list[dict] = []

    def start(self) -> "StubServer":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") == "/v1/models":
                    self._send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in stub.models]})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                stub.requests.append(req)
                if stub.delay:
                    time.sleep(stub.delay)
                if stub.reject_schema and "response_format" in req:
                    return self._send(400, {"error": "response_format is not supported"})
                user = req["messages"][-1]["content"]
                has_image = isinstance(user, list) and any(p.get("type") == "image_url" for p in user)
                if has_image and stub.reject_images:
                    return self._send(400, {"error": "this model does not take images"})
                text = user if isinstance(user, str) else " ".join(p.get("text", "") for p in user if p.get("type") == "text")
                content = json.dumps(_default_answer(text, has_image))
                self._send(200, {"id": "c1", "object": "chat.completion",
                                 "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": max(1, len(text) // 4), "completion_tokens": max(1, len(content) // 4)}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub():
    s = StubServer().start()
    yield s
    s.stop()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("COA_LLM_BASE_URL", "COA_LLM_MODEL", "COA_LLM_API_KEY", "COA_LLM_JSON_SCHEMA", "COA_LLM_TIMEOUT",
              "COA_LLM_CONNECT_TIMEOUT", "COA_LLM_ALLOW_REMOTE", "COA_LLM_LOG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


SCHEMA = {"type": "object", "properties": {"sum": {"type": "integer"}}, "required": ["sum"]}


def test_local_endpoints_are_recognised():
    for url in ("http://127.0.0.1:8000/v1", "http://localhost:11434/v1", "http://10.2.3.4:8000/v1", "http://192.168.1.20/v1",
                "http://[::1]:8000/v1", "http://100.101.102.103:8000/v1", "http://gpu-box.local:8000/v1"):
        assert is_local_url(url), url
    for url in ("https://8.8.8.8/v1", "https://1.1.1.1:443/v1", "not a url"):
        assert not is_local_url(url), url
    assert not is_local_url("https://api.example.com/v1", resolve=False)


def test_default_endpoint_is_the_local_server(monkeypatch):
    monkeypatch.setenv("COA_LLM_MODEL", "m")
    llm = from_env(check=False)
    assert llm.base_url == LOCAL_DEFAULT_URL and llm.local and llm.model == "m"


def test_model_is_taken_from_the_server(stub, monkeypatch):
    monkeypatch.setenv("COA_LLM_BASE_URL", stub.url)
    llm = from_env()
    assert llm.model == "stub-model"
    assert llm.describe()["local"] is True


def test_remote_endpoint_needs_explicit_consent(monkeypatch):
    monkeypatch.setenv("COA_LLM_BASE_URL", "https://8.8.8.8/v1")
    monkeypatch.setenv("COA_LLM_MODEL", "m")
    with pytest.raises(LLMConfigError):
        from_env(check=False)
    monkeypatch.setenv("COA_LLM_ALLOW_REMOTE", "1")
    llm = from_env(check=False)
    assert llm.local is False


def test_schema_constrained_call_records_usage(stub):
    llm = OpenAICompatibleClient(stub.url, "stub-model")
    assert llm.complete_json("calc", "What is 17 + 25?", SCHEMA, stage="L2") == {"sum": 42}
    assert stub.requests[-1]["response_format"]["type"] == "json_schema"
    assert stub.requests[-1]["temperature"] == 0.0
    assert llm.usage.calls == 1 and llm.usage.by_stage["L2"]["output_tokens"] > 0
    assert llm.schema_rejected == 0


def test_server_without_schema_support_gets_a_plain_json_prompt():
    s = StubServer(reject_schema=True).start()
    try:
        llm = OpenAICompatibleClient(s.url, "stub-model")
        assert llm.complete_json("calc", "What is 17 + 25?", SCHEMA) == {"sum": 42}
        assert "response_format" in s.requests[0] and "response_format" not in s.requests[1]
        assert llm.schema_rejected == 1 and llm.down is None
    finally:
        s.stop()


def test_partial_answers_are_caught_before_the_role_check():
    """A server that refused the schema answers a plain JSON prompt, so an empty or partial record must not pass."""
    schema = {"type": "object", "properties": {"comparison": {"type": "string"}, "concerns": {"type": "array"}},
              "required": ["comparison", "concerns"]}
    assert schema_problems({}, schema) == ["field 'comparison' is missing", "field 'concerns' is missing"]
    assert schema_problems({"comparison": 3, "concerns": []}, schema) == ["field 'comparison' is not a string"]
    assert schema_problems([], schema) == ["the answer is not a JSON object"]
    assert schema_problems({"comparison": "C1 goes first.", "concerns": []}, schema) == []
    prompts = []

    def responder(stage, user):
        prompts.append(user)
        return {}

    value, problems = call_validated(ScriptedLLM(responder), "s", "u", schema, "coa", lambda o: (o, []))
    assert value is None and len(prompts) == 2 and "missing" in problems[0]
    assert "was rejected: field 'comparison' is missing" in prompts[1]


def test_server_down_fails_fast_and_stays_down():
    llm = OpenAICompatibleClient(f"http://127.0.0.1:{_free_port()}/v1", "m", connect_timeout=1.0)
    t = time.perf_counter()
    with pytest.raises(LLMUnavailable):
        llm.complete_json("s", "u", SCHEMA)
    assert time.perf_counter() - t < 2.0
    assert llm.down
    t = time.perf_counter()
    value, problems = call_validated(llm, "s", "u", SCHEMA, "L6", lambda o: (o, []))
    assert value is None and problems and problems[0].startswith("call failed")
    assert time.perf_counter() - t < 0.5  # no new connection attempt once the server is marked down


def test_hung_server_times_out_and_later_roles_fall_back_at_once():
    s = StubServer(delay=3.0).start()
    try:
        llm = OpenAICompatibleClient(s.url, "stub-model", timeout=0.5)
        t = time.perf_counter()
        with pytest.raises(LLMUnavailable):
            llm.complete_json("calc", "What is 17 + 25?", SCHEMA)
        assert time.perf_counter() - t < 2.5
        n = len(s.requests)
        with pytest.raises(LLMUnavailable):
            llm.complete_json("calc", "What is 17 + 25?", SCHEMA)
        assert len(s.requests) == n
    finally:
        s.stop()


def test_image_reaches_the_server_and_the_local_log_leaves_it_out(stub, tmp_path):
    log = tmp_path / "outputs" / "llm.jsonl"  # the folder does not exist yet: the client creates it
    llm = OpenAICompatibleClient(stub.url, "stub-model", log_path=str(log))
    url = "data:image/png;base64," + "A" * 5000
    out = llm.complete_json_images("describe", "What colour is the square?", {"type": "object"}, [url], stage="L9")
    assert out == {"colour": "red"}
    parts = stub.requests[-1]["messages"][-1]["content"]
    assert any(p["type"] == "image_url" and p["image_url"]["url"] == url for p in parts)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["stage"] == "L9" and rec["answer"] == json.dumps({"colour": "red"})
    assert "AAAA" not in json.dumps(rec) and "<image, 5022 characters>" in json.dumps(rec)


def test_llm_check_reports_on_the_local_server(stub, monkeypatch, tmp_path, capsys):
    from coa_net.cli import main

    monkeypatch.setenv("COA_LLM_BASE_URL", stub.url)
    main(["llm-check", "--out", str(tmp_path / "check.json")])
    text = capsys.readouterr().out
    assert "(local)" in text and "image (L9)" in text
    assert re.search(r"JSON answer \(schema-constrained\)\s+ok: sum = 42", text), text
    rep = json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))
    assert rep["server"]["model"] == "stub-model" and rep["served_models"] == ["stub-model"]
    assert all(c["ok"] for c in rep["checks"].values()), rep["checks"]


def test_llm_check_without_image_support_still_passes_for_text(monkeypatch, tmp_path, capsys):
    from coa_net.cli import main

    s = StubServer(reject_images=True).start()
    try:
        monkeypatch.setenv("COA_LLM_BASE_URL", s.url)
        main(["llm-check", "--out", str(tmp_path / "check.json")])
        rep = json.loads((tmp_path / "check.json").read_text(encoding="utf-8"))
        assert rep["checks"]["JSON answer"]["ok"] and not rep["checks"]["image (L9)"]["ok"]
        assert "position words" in capsys.readouterr().out
    finally:
        s.stop()


def test_llm_flag_stops_with_advice_when_no_server_answers(monkeypatch, tmp_path):
    from coa_net.cli import main

    monkeypatch.setenv("COA_LLM_BASE_URL", f"http://127.0.0.1:{_free_port()}/v1")
    with pytest.raises(SystemExit) as e:
        main(["demo", "--llm", "--reps", "0", "--out", str(tmp_path / "demo")])
    assert "llm-check" in str(e.value) and "deterministic fallback" in str(e.value)


def test_report_and_metrics_say_where_the_model_ran(stub):
    from coa_net.pipeline import COANet
    from coa_net.scenarios import sample_text, synthetic_theatre

    llm = OpenAICompatibleClient(stub.url, "stub-model")
    session = COANet.from_graph(synthetic_theatre(0).G, llm=llm, sim_reps=0, explain=False, recce=False)
    session.plan(sample_text("sample_order.txt"))  # the stand-in answers {} to role prompts: every role falls back
    line = session._model_line()
    assert "stub-model" in line and "(local)" in line and f"{llm.usage.calls} calls" in line
    assert llm.usage.calls > 0
    assert session.metrics()["llm_usage"]["server"]["local"] is True
    assert "Language model: none" in COANet.from_graph(synthetic_theatre(0).G, sim_reps=0)._model_line()

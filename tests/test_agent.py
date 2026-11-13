"""Tests der Agenten-Schleife gegen einen simulierten Claude-Server (kein API-Schlüssel nötig).

Der Fake-Server spricht dasselbe Streaming-Format (SSE) wie die echte API. So wird
der komplette Weg geprüft: SDK-Streaming -> Werkzeug ausführen -> Ergebnis zurück.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anthropic
import pytest

from lamar.agent import Agent, sanitize_after_fallback


# -- Fake-Server ---------------------------------------------------------------


def sse_response(blocks: list[dict], stop_reason: str = "end_turn", model: str = "claude-opus-5-5",
                 stop_details: dict | None = None) -> bytes:
    events: list[tuple[str, dict]] = [(
        "message_start",
        {"type": "message_start", "message": {
            "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 100, "output_tokens": 1,
                      "cache_creation_input_tokens": 10, "cache_read_input_tokens": 50}}},
    )]
    for i, b in enumerate(blocks):
        deltas: list[dict] = []
        if b["type"] == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": b["text"]}]
        elif b["type"] == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": b["thinking"]},
                      {"type": "signature_delta", "signature": "c2ln"}]
        elif b["type"] == "tool_use":
            start = {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": b["json"]}]
        elif b["type"] == "fallback":
            start = {"type": "fallback", "from": {"model": "claude-opus-5-5"},
                     "to": {"model": "claude-opus-4-8"}, "trigger": {"category": "cyber"}}
        else:
            raise ValueError(b)
        events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        for d in deltas:
            events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": d}))
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details:
        delta["stop_details"] = stop_details
    events.append(("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": 42}}))
    events.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


class FakeClaude:
    def __init__(self, responses: list[bytes]):
        self.responses = list(responses)
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", 0))
                fake.requests.append(json.loads(self.rfile.read(length)))
                fake.headers.append(dict(self.headers))
                body = fake.responses.pop(0)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def client(self) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key="test-key", base_url=self.url, max_retries=0)

    def close(self):
        self.server.shutdown()


class RecordingUI:
    def __init__(self, approve_answer: str = "yes"):
        self.events: list[tuple] = []
        self.approve_answer = approve_answer

    def status(self, message): pass
    def text(self, delta): self.events.append(("text", delta))
    def thinking(self, delta): self.events.append(("thinking", delta))
    def block_end(self): pass
    def tool_call(self, name, tool_input): self.events.append(("tool_call", name, tool_input))
    def tool_result(self, name, outcome): self.events.append(("tool_result", name, outcome.is_error))
    def show_change(self, kind, title, detail): self.events.append(("change", kind, title))
    def approve(self, kind, title, detail):
        self.events.append(("approve", kind, title))
        return self.approve_answer
    def info(self, message): self.events.append(("info", message))
    def warn(self, message): self.events.append(("warn", message))

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for var in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


def make_agent(tmp_path: Path, responses: list[bytes], **kwargs):
    fake = FakeClaude(responses)
    ui = RecordingUI(kwargs.pop("approve_answer", "yes"))
    agent = Agent(tmp_path, ui, client=fake.client(), **kwargs)
    return agent, ui, fake


# -- Tests -------------------------------------------------------------------


def test_tool_loop_writes_file_and_sends_result_back(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([
            {"type": "thinking", "thinking": "Ich lege die Datei an."},
            {"type": "text", "text": "Ich erstelle hallo.py."},
            {"type": "tool_use", "id": "toolu_1", "name": "write_file",
             "json": json.dumps({"path": "hallo.py", "content": "print('Hallo Welt')\n"})},
        ], stop_reason="tool_use"),
        sse_response([{"type": "text", "text": "Fertig!"}]),
    ])
    try:
        agent.run("Schreib ein Hallo-Welt-Programm")
    finally:
        fake.close()

    assert (tmp_path / "hallo.py").read_text() == "print('Hallo Welt')\n"
    assert ("tool_call", "write_file", {"path": "hallo.py", "content": "print('Hallo Welt')\n"}) in ui.events
    assert ("tool_result", "write_file", False) in ui.events
    assert "".join(e[1] for e in ui.of("text")) == "Ich erstelle hallo.py.Fertig!"

    # Erste Anfrage: Konfiguration prüfen
    first = fake.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "high"}
    assert first["fallbacks"] == "default"
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["context_management"] == {"edits": [{"type": "compact_20260112"}]}
    assert first["stream"] is True
    assert "tool_choice" not in first
    custom = [t for t in first["tools"] if "input_schema" in t]
    assert custom and all(t["eager_input_streaming"] is True for t in custom)
    assert {"type": "web_search_20260209", "name": "web_search"} in first["tools"]
    betas = fake.headers[0].get("anthropic-beta", "")
    assert "server-side-fallback-2026-07-01" in betas and "compact-2026-01-12" in betas

    # Zweite Anfrage: Verlauf korrekt angehängt (Assistent-Inhalt unverändert + tool_result)
    second = fake.requests[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assistant = second[1]["content"]
    assert [b["type"] for b in assistant] == ["thinking", "text", "tool_use"]
    assert assistant[0]["signature"] == "c2ln"
    assert all("parsed_output" not in b for b in assistant)
    result = second[2]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_1"
    assert "is_error" not in result

    assert agent.usage.requests == 2
    assert agent.usage.output_tokens == 84
    assert agent.usage.cost_usd > 0


def test_commands_need_approval_in_normal_mode(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([{"type": "tool_use", "id": "toolu_1", "name": "run_command",
                       "json": json.dumps({"command": "echo nein > x.txt"})}], stop_reason="tool_use"),
        sse_response([{"type": "text", "text": "Ok, nicht ausgeführt."}]),
    ], approve_answer="no")
    try:
        agent.run("lösch alles")
    finally:
        fake.close()
    assert ("approve", "command", "Befehl ausführen") in ui.events
    assert not (tmp_path / "x.txt").exists()
    result = fake.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "abgelehnt" in result["content"]


def test_refusal_rolls_back_turn(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([], stop_reason="refusal",
                     stop_details={"type": "refusal", "category": "cyber", "explanation": None}),
    ])
    try:
        agent.run("etwas Böses")
    finally:
        fake.close()
    assert agent.messages == []
    assert any("abgelehnt" in e[1] and "cyber" in e[1] for e in ui.of("warn"))


def test_max_tokens_tool_input_is_not_executed(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([{"type": "tool_use", "id": "toolu_1", "name": "write_file",
                       "json": '{"path": "big.py", "content": "abc'}], stop_reason="max_tokens"),
        sse_response([{"type": "text", "text": "Ich teile es auf."}]),
    ])
    try:
        agent.run("schreib eine riesige Datei")
    finally:
        fake.close()
    assert not (tmp_path / "big.py").exists()
    result = fake.requests[1]["messages"][2]["content"][0]
    assert result["is_error"] is True and "output length limit" in result["content"]


def test_mid_stream_fallback_drops_declined_partial(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Teil 1 "},
            {"type": "tool_use", "id": "toolu_declined", "name": "write_file",
             "json": json.dumps({"path": "nein.txt", "content": "x"})},
            {"type": "fallback"},
            {"type": "text", "text": "Weiter mit anderem Modell."},
        ], model="claude-opus-4-8"),
        sse_response([{"type": "text", "text": "ok"}]),
    ])
    try:
        agent.run("Frage 1")
        agent.run("Frage 2")
    finally:
        fake.close()
    assert not (tmp_path / "nein.txt").exists()
    echoed = fake.requests[1]["messages"][1]["content"]
    assert [b["type"] for b in echoed] == ["text", "fallback", "text"]
    assert any("übernimmt" in e[1] for e in ui.of("info"))


def test_sanitize_without_fallback_is_identity():
    class B:
        def __init__(self, t):
            self.type = t
    blocks = [B("thinking"), B("text"), B("tool_use")]
    assert sanitize_after_fallback(blocks) == blocks


def test_keyboard_interrupt_during_tools_closes_open_calls(tmp_path: Path):
    agent, ui, fake = make_agent(tmp_path, [
        sse_response([
            {"type": "tool_use", "id": "toolu_a", "name": "list_directory", "json": "{}"},
            {"type": "tool_use", "id": "toolu_b", "name": "list_directory", "json": "{}"},
        ], stop_reason="tool_use"),
    ])
    calls = {"n": 0}
    original = agent.toolbox.execute

    def interrupting(name, raw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return original(name, raw)

    agent.toolbox.execute = interrupting
    try:
        agent.run("zeig mir die Dateien")
    finally:
        fake.close()
    last = agent.messages[-1]
    assert last["role"] == "user"
    ids = [(r["tool_use_id"], r.get("is_error", False)) for r in last["content"]]
    assert ids == [("toolu_a", False), ("toolu_b", True)]
    assert any("Unterbrochen" in e[1] for e in ui.of("warn"))

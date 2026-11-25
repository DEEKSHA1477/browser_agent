"""Ollama provider (OpenAI-compatible API) and Gemini -> Ollama fallback, against a fake local server."""
import http.server
import json
import threading

import pytest

from services import llm_service as ls

received = []


class _FakeOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        received.append((self.path, body))
        reply = {"id": "x", "object": "chat.completion", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": '{"action": {"name": "done"}}'}}]}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_ollama(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    settings = ls.settings.__class__.__new__(ls.settings.__class__)
    settings.__dict__.update(ls.settings.__dict__)
    object.__setattr__(settings, "ollama_base_url", f"http://127.0.0.1:{server.server_address[1]}")
    object.__setattr__(settings, "ollama_model", "qwen3:8b")
    monkeypatch.setattr(ls, "settings", settings)
    received.clear()
    yield settings
    server.shutdown()


async def test_ollama_client_json(fake_ollama):
    out = await ls.OllamaClient().complete_json("system rules", "page text", image=b"jpeg")
    assert out == {"action": {"name": "done"}}
    path, body = received[0]
    assert path == "/v1/chat/completions" and body["model"] == "qwen3:8b"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][0] == {"role": "system", "content": "system rules"}
    assert body["messages"][1]["content"] == "page text"  # no image unless OLLAMA_VISION=true


async def test_ollama_not_running_gives_clear_error(fake_ollama):
    object.__setattr__(fake_ollama, "ollama_base_url", "http://127.0.0.1:9")
    with pytest.raises(ls.LLMError, match="Cannot reach Ollama"):
        await ls.OllamaClient().complete_json("s", "p")


async def test_gemini_falls_back_to_ollama(fake_ollama):
    class DownGemini:
        async def complete_json(self, system, prompt, image=None):
            raise ls.LLMError("All Gemini models are unavailable right now: 429")

    client = ls.FallbackClient(DownGemini(), ls.OllamaClient(), "ollama")
    assert await client.complete_json("s", "p") == {"action": {"name": "done"}}
    assert len(received) == 1


def test_ollama_needs_no_key():
    s = ls.settings.__class__.__new__(ls.settings.__class__)
    s.__dict__.update(ls.settings.__dict__)
    object.__setattr__(s, "llm_provider", "ollama")
    assert s.llm_configured and s.llm_model == s.ollama_model

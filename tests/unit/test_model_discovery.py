"""Local model discovery: Ollama and OpenAI-compatible servers on loopback only, vision inferred,
silence reported; nothing remote is contacted and no key is sent."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any

import pytest

from highhx.models import discovery


def server(routes: dict[str, Any]) -> tuple[http.server.ThreadingHTTPServer, list[dict[str, str]]]:
    seen: list[dict[str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, "auth": self.headers.get("Authorization") or ""})
            body = json.dumps(routes.get(self.path, {})).encode()
            self.send_response(200 if self.path in routes else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_ollama_and_openai_compatible_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    ollama, ollama_seen = server(
        {
            "/api/tags": {
                "models": [
                    {"name": "qwen2.5vl:3b", "size": 3_000_000_000},
                    {"name": "llama3.2:3b", "details": {"families": ["llama"]}},
                ]
            }
        }
    )
    vllm, vllm_seen = server({"/v1/models": {"data": [{"id": "Qwen2-VL-7B"}, {"id": "mistral-7b"}]}})
    monkeypatch.setattr(discovery, "OLLAMA", f"http://127.0.0.1:{ollama.server_address[1]}")
    monkeypatch.setenv("HIGHHX_PLANNER_BASE_URL", f"http://127.0.0.1:{vllm.server_address[1]}/v1")
    monkeypatch.setenv("HIGHHX_VISION_BASE_URL", "https://vision.example.com/v1")  # remote: never contacted
    monkeypatch.setenv("OPENAI_API_KEY", "sk-never-sent")
    found, silent = discovery.discover()
    by_name = {m.model: m for m in found}
    assert (
        by_name["qwen2.5vl:3b"].vision
        and by_name["qwen2.5vl:3b"].server == "ollama"
        and not by_name["llama3.2:3b"].vision
    )
    assert (
        by_name["Qwen2-VL-7B"].vision
        and not by_name["mistral-7b"].vision
        and by_name["mistral-7b"].server == "openai-compatible"
    )
    assert silent == {} and all(not r["auth"] for r in ollama_seen + vllm_seen)  # no key is ever sent
    assert "https://vision.example.com/v1" not in discovery.endpoints()


def test_nothing_answering_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery, "OLLAMA", "http://127.0.0.1:9")
    monkeypatch.delenv("HIGHHX_PLANNER_BASE_URL", raising=False)
    monkeypatch.delenv("HIGHHX_VISION_BASE_URL", raising=False)
    found, silent = discovery.discover(timeout=0.5)
    assert found == [] and list(silent) == ["http://127.0.0.1:9"]

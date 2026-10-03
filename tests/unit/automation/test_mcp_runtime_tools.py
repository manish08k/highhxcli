"""The runtime toolset of HighhX's MCP server: every call is an ActionRequest through the one
executor (risk, policy, approval, verification, audit), with tool.* events and its own trace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.mcp import RUNTIME_TOOLS, TOOLS, McpServer
from highhx.core.errors import HighhXError


def call(server: McpServer, name: str, arguments: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
    response = server.handle({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response is not None
    return response["result"]


def test_toolsets(agent_project: Path, executor_for) -> None:
    executor, _ = executor_for(agent_project)
    desktop = McpServer(executor)
    assert set(desktop.specs) == {t for t in TOOLS if executor.catalog.get(f"computer.{t}")}
    runtime = McpServer(executor, toolsets=("runtime",))
    assert set(runtime.specs) == set(RUNTIME_TOOLS) and all(executor.catalog.get(a) for a in RUNTIME_TOOLS.values())
    both = McpServer(executor, toolsets=("desktop", "runtime"))
    assert "sandbox_exec" in both.specs and "click_at" in both.specs
    read_only = McpServer(executor, toolsets=("runtime",), read_only=True)
    assert "sandbox_list" in read_only.specs and "sandbox_exec" not in read_only.specs and "android_tap" not in read_only.specs
    with pytest.raises(HighhXError):
        McpServer(executor, toolsets=("everything",))
    listed = runtime.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    tool = next(t for t in listed["result"]["tools"] if t["name"] == "api_request")
    assert tool["title"] == "api.request" and "url" in tool["inputSchema"]["properties"]


def test_calls_go_through_the_executor_with_events_and_a_trace(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    events: list[Any] = []
    executor.events.subscribe("*", events.append)
    server = McpServer(executor, toolsets=("runtime",))
    listed = call(server, "sandbox_list", {})
    assert not listed["isError"] and listed["structuredContent"]["outcome"] == "success"
    created = call(server, "sandbox_create", {"isolation": "workspace"})  # weaker isolation: asked (approved here)
    assert not created["isError"] and ui.requests
    sandbox_id = created["structuredContent"]["output"]["id"]
    ran = call(server, "sandbox_exec", {"id": sandbox_id, "command": "echo from-mcp"})
    assert not ran["isError"] and "from-mcp" in ran["structuredContent"]["output"]["stdout"]
    assert ran["structuredContent"]["risk"] in ("low", "medium") and ran["structuredContent"]["trace_id"].startswith("tr_")
    call(server, "sandbox_destroy", {"id": sandbox_id})
    tool_events = [e for e in events if e.name.startswith("tool.")]
    assert [e.name for e in tool_events][:2] == ["tool.started", "tool.completed"]
    assert all(e.context["source"] == "mcp" and e.context["session_id"] == server.session_id for e in tool_events)
    planned = next(e for e in events if e.name == "action.planned" and e.data["action"] == "sandbox.exec")
    assert planned.context["source"] == "mcp" and planned.context["action_id"]


def test_refusals_and_declines_are_reported(agent_project: Path, executor_for) -> None:
    executor, ui = executor_for(agent_project)
    server = McpServer(executor, toolsets=("runtime",), allow=["sandbox_list", "api_request"])
    assert call(server, "sandbox_exec", {"id": "x", "command": "ls"})["isError"]
    invalid = call(server, "api_request", {})
    assert invalid["isError"] and invalid["structuredContent"]["status"] == "invalid"
    ui.action_answers = [False]
    declined = call(server, "api_request", {"url": "https://example.invalid/", "method": "DELETE"})
    assert declined["isError"] and declined["structuredContent"]["status"] == "denied"


@pytest.mark.e2e
def test_highhx_mcp_serve_over_stdio(tmp_path: Path) -> None:
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "sandbox_list", "arguments": {}}},
    ]
    completed = subprocess.run(
        [sys.executable, "-m", "highhx", "mcp", "serve", "--toolset", "runtime", "--mode", "read-only"],
        cwd=tmp_path,
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HIGHHX_AUTOMATION_ENGINE": "python"},
        check=False,
    )
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert completed.returncode == 0, completed.stderr
    assert responses[0]["result"]["serverInfo"]["name"] == "highhx-computer"
    names = {t["name"] for t in responses[1]["result"]["tools"]}
    assert "computer_state" in names and "sandbox_exec" not in names  # read-only
    assert responses[2]["result"]["isError"] is False

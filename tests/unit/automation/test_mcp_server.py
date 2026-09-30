"""The MCP surface (``highhx computer mcp``): JSON-RPC over stdio in front of the same action
executor and gate as ``highhx computer`` — tool discovery with schemas, structured results and
errors, and permission fixed when the server starts (read-only, a bounded tool set, --yes)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.guidance import COMPUTER_USE_GUIDANCE
from highhx.computer.mcp import PROTOCOL_VERSIONS, TOOLS, McpServer
from highhx.core.errors import HighhXError
from tests.unit.automation.fakes import FakeEngine


def server(executor_for: Any, root: Path, **kwargs: Any) -> McpServer:
    options = {k: kwargs.pop(k) for k in ("yes",) if k in kwargs}
    executor, _ = executor_for(root, interactive=False, **options)
    return McpServer(executor, version="test", **kwargs)


def call(srv: McpServer, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    response = srv.handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}
    )
    assert response is not None
    return response["result"]


def test_initialize_negotiates_the_version_and_carries_the_guidance(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    srv = server(executor_for, agent_project)
    old = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
    assert old is not None and old["result"]["protocolVersion"] == "2024-11-05"
    new = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": "2099-01-01"}})
    assert new is not None and new["result"]["protocolVersion"] == PROTOCOL_VERSIONS[0]
    assert new["result"]["instructions"] == COMPUTER_USE_GUIDANCE
    assert new["result"]["capabilities"] == {"tools": {"listChanged": False}}


def test_tools_are_the_desktop_catalog_actions_with_their_schemas(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    srv = server(executor_for, agent_project)
    response = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    tools = {t["name"]: t for t in response["result"]["tools"]}
    assert list(tools) == list(TOOLS)
    assert tools["click_at"]["inputSchema"]["properties"]["x"]["type"] == "integer"
    assert tools["verify"]["inputSchema"]["required"] == ["expect"]
    assert "source" not in tools["scroll"]["inputSchema"]["properties"]  # always the desktop here
    assert tools["windows"]["annotations"]["readOnlyHint"] and not tools["click_at"]["annotations"]["readOnlyHint"]
    assert tools["quit"]["annotations"]["destructiveHint"]


def test_read_only_and_bounded_tool_sets(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    reads = server(executor_for, agent_project, read_only=True)
    assert set(reads.specs) == {"observe", "screenshot", "windows", "apps", "element_at", "verify", "clipboard_read"}
    refused = call(reads, "click_at", {"x": 1, "y": 1})
    assert refused["isError"] and refused["structuredContent"]["status"] == "refused" and engine.sent("click_at") == []
    bounded = server(executor_for, agent_project, allow=["windows", "move"])
    assert set(bounded.specs) == {"windows", "move"}
    with pytest.raises(HighhXError, match="Unknown tool"):
        server(executor_for, agent_project, allow=["rm_rf"])


def test_a_call_returns_the_action_result(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    srv = server(executor_for, agent_project)
    result = call(srv, "windows")
    assert not result["isError"] and result["structuredContent"]["output"]["windows"][0]["id"] == 7
    assert result["content"][0]["text"].startswith("1 window(s)")
    moved = call(srv, "move", {"x": 5, "y": 6})  # low risk, the person's own server: runs
    assert not moved["isError"] and engine.pointer == (5, 6)


def test_actions_that_need_a_confirmation_are_refused_without_yes(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    srv = server(executor_for, agent_project)
    denied = call(srv, "click_at", {"x": 140, "y": 115})
    assert denied["isError"] and denied["structuredContent"]["status"] == "denied"
    assert "--yes" in denied["content"][0]["text"] and engine.sent("click_at") == []
    approved = server(executor_for, agent_project, yes=True)
    clicked = call(approved, "click_at", {"x": 140, "y": 115})
    assert not clicked["isError"] and engine.sent("click_at") == [
        ("click_at", {"x": 140, "y": 115, "button": "left", "count": 1})
    ]


def test_invalid_arguments_and_unknown_tools_are_structured_errors(
    agent_project: Path, executor_for, engine: FakeEngine
) -> None:
    srv = server(executor_for, agent_project)
    invalid = call(srv, "move", {"x": "left"})
    assert invalid["isError"] and invalid["structuredContent"]["status"] == "invalid"
    assert invalid["structuredContent"]["details"]
    unknown = call(srv, "format_disk")
    assert unknown["isError"] and "unknown" in unknown["structuredContent"]["error"]


def test_a_screenshot_is_returned_as_an_image(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    result = call(server(executor_for, agent_project), "screenshot")
    assert not result["isError"] and result["content"][1]["type"] == "image"
    assert result["content"][1]["mimeType"] == "image/png" and result["content"][1]["data"]


def test_the_stdio_loop(agent_project: Path, executor_for, engine: FakeEngine) -> None:
    srv = server(executor_for, agent_project)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "apps", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": 3}},
        {"id": 6, "method": "ping"},
    ]
    reader = io.StringIO("\n".join(json.dumps(m) for m in lines) + "\n{broken\n\n")
    writer = io.StringIO()
    assert srv.serve(reader, writer) == 0
    out = [json.loads(line) for line in writer.getvalue().splitlines()]
    assert [m.get("id") for m in out] == [1, 2, 3, 4, 5, 6, None]  # no answer to the notification
    assert out[1]["result"] == {}
    assert [m["error"]["code"] for m in out[2:] if "error" in m] == [-32601, -32602, -32600, -32700]
    assert not out[3]["result"]["isError"]

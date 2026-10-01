"""External MCP servers mounted into the agent: their tools in the registry, called through the
session's approval gate and audit — against a real server process (HighhX's own, read-only)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

SERVER = [sys.executable, "-m", "highhx", "computer", "mcp", "--mode", "read-only"]


def test_the_agent_calls_a_mounted_tool_through_the_gate(
    agent_project: Path, make_session: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.unit.agent.conftest import reply

    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcpServers": {"desktop": {"command": SERVER[0], "args": SERVER[1:]}}}))
    monkeypatch.setenv("HIGHHX_MCP_CONFIG", str(config))
    steps = [reply("", [("mcp__desktop__apps", {})]), reply("Listed.")]
    session, provider, _ = make_session(agent_project, steps)
    try:
        assert "mcp__desktop__apps" in session.registry
        assert session.run_turn("which apps are running?").stopped == "completed"
        answer = provider.requests[1].messages[-1].tool_results[0]
        assert not answer.is_error and "application(s)" in answer.content
        assert "external MCP server 'desktop'" in session.registry.get("mcp__desktop__apps").description
    finally:
        session.close()

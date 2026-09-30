"""`highhx computer mcp` as a real subprocess: stdout carries only JSON-RPC, discovery works, and
the launch-time mode binds every call. It never sends input to the desktop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e


def mcp(messages: list[dict[str, Any]], *args: str, cwd: Path) -> tuple[list[dict[str, Any]], str, int]:
    completed = subprocess.run(
        [sys.executable, "-m", "highhx", "computer", "mcp", *args],
        cwd=cwd,
        input="".join(json.dumps(m) + "\n" for m in messages),
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HIGHHX_AUTOMATION_ENGINE": "python"},
        check=False,
    )
    lines = completed.stdout.splitlines()
    return [json.loads(line) for line in lines], completed.stderr, completed.returncode


def test_read_only_server_over_stdio(tmp_path: Path) -> None:
    responses, stderr, code = mcp(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "type", "arguments": {"text": "x"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "verify", "arguments": {}}},
        ],
        "--mode",
        "read-only",
        cwd=tmp_path,
    )
    assert code == 0, stderr
    assert [r["id"] for r in responses] == [1, 2, 3, 4]  # every stdout line is a protocol message
    assert responses[0]["result"]["serverInfo"]["name"] == "highhx-computer"
    names = {t["name"] for t in responses[1]["result"]["tools"]}
    assert "observe" in names and "verify" in names and not names & {"type", "click_at", "quit"}
    assert responses[2]["result"]["isError"] and responses[2]["result"]["structuredContent"]["status"] == "refused"
    invalid = responses[3]["result"]
    assert invalid["isError"] and invalid["structuredContent"]["status"] == "invalid"


def test_a_bounded_tool_set_and_bad_flags(tmp_path: Path) -> None:
    responses, _stderr, code = mcp(
        [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}], "--allow", "windows", "--allow", "apps", cwd=tmp_path
    )
    assert code == 0 and [t["name"] for t in responses[0]["result"]["tools"]] == ["windows", "apps"]
    _responses, stderr, code = mcp([], "--allow", "nope", cwd=tmp_path)
    assert code != 0 and "Unknown tool" in stderr

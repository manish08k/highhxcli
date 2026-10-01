"""Connections that cross a process boundary, run for real (read-only: nothing is typed or clicked):

- HighhX's MCP client mounting an MCP server — HighhX's own ``highhx computer mcp`` — and the agent
  calling one of its tools through the approval gate;
- a remote computer's engine over a stdio transport (the one SSH carries): the same protocol, a
  lost connection reported as such, and a new connection with everything stale discarded."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.protocol import ProtocolError
from highhx.automation.engine.remote import RemoteEngine, RemoteTarget, parse_target, ssh_argv
from highhx.computer.driver import HighhXDriver
from highhx.integrations.mcp_client import CONNECTED, FAILED, McpManager, ServerConfig, load_servers

pytestmark = pytest.mark.e2e
SERVER = ServerConfig("desktop", sys.executable, ("-m", "highhx", "computer", "mcp", "--mode", "read-only"))


def test_mounting_an_mcp_server(tmp_path: Path) -> None:
    manager = McpManager([SERVER, ServerConfig("missing", "definitely-not-a-command-xyz")])
    try:
        problems = manager.connect_all()
        rows = {r["server"]: r for r in manager.status()}
        assert rows["desktop"]["state"] == CONNECTED and rows["missing"]["state"] == FAILED
        assert problems and "not installed" in problems[0]
        names = {t.name for t in manager.tools()}
        assert {"observe", "windows", "verify"} <= names and "click_at" not in names  # read-only mode
        assert all(t.read_only for t in manager.tools())
        client = manager.clients["desktop"]
        result = client.call("verify", {})  # invalid input: a structured error, not a crash
        assert result.error and "invalid" in result.text
        client.close()
        assert client.health() != CONNECTED
        assert not client.call("apps", {}).error  # restarted for a new call
    finally:
        manager.close()


def test_configured_servers_are_read_from_both_places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file = tmp_path / "servers.json"
    file.write_text(json.dumps({"mcpServers": {"b": {"command": ["npx", "-y"], "args": ["srv"], "env": {"T": "${HOME}"}}}}))
    monkeypatch.setenv("HIGHHX_MCP_CONFIG", str(file))
    servers = load_servers({"mcp": {"servers": {"a": {"command": "a-server", "enabled": False}}}})
    by_name = {s.name: s for s in servers}
    assert by_name["b"].argv() == ["npx", "-y", "srv"] and by_name["b"].environment()["T"]
    assert not by_name["a"].enabled


# ------------------------------------------------------------ remote computer
def test_ssh_targets_and_the_command_they_run() -> None:
    target = parse_target("ssh://me@studio.local:2222?highhx=/opt/bin/highhx")
    argv = ssh_argv(target)
    assert argv[:2] == ["ssh", "-T"] and "BatchMode=yes" in argv and ["-p", "2222"] == argv[argv.index("-p") : argv.index("-p") + 2]
    assert argv[-5:] == ["me@studio.local", "--", "/opt/bin/highhx", "computer", "engine"]
    for bad in ("http://host", "ssh://", "ssh://h?highhx=x;rm"):
        with pytest.raises(Exception):
            parse_target(bad)


def _remote() -> tuple[RemoteEngine, HighhXDriver]:
    engine = RemoteEngine(RemoteTarget("loopback"), argv=[sys.executable, "-m", "highhx", "computer", "engine"])
    return engine, HighhXDriver(AutomationBridge(engine), target="ssh://loopback")


def test_a_remote_engine_speaks_the_protocol_and_reports_a_lost_connection() -> None:
    engine, driver = _remote()
    try:
        assert engine.protocol == 2 and driver.connected()
        assert driver.screen().width > 0 and isinstance(driver.windows(), list)
        with pytest.raises(ProtocolError, match="screenshots"):
            driver.call("screenshot", path="/tmp/not-allowed.png")  # this computer's contract still applies
        engine._process.kill()
        engine._process.wait()
        assert not driver.connected()
        with pytest.raises(EngineError) as lost:
            driver.windows()
        assert lost.value.code == "connection_lost"
    finally:
        engine.close()


def test_the_session_reconnects_and_forgets_what_the_old_connection_saw(monkeypatch: pytest.MonkeyPatch) -> None:
    from highhx.computer.session import ComputerSession

    from types import SimpleNamespace

    session = ComputerSession(SimpleNamespace(engine=None), actor="agent", target="ssh://loopback")  # type: ignore[arg-type]
    made: list[RemoteEngine] = []

    def open_remote(runner: Any, *, cancel: Any = None, target: str = "local") -> AutomationBridge:
        engine, _driver = _remote()
        made.append(engine)
        return AutomationBridge(engine)

    monkeypatch.setattr("highhx.automation.engine.bridge.open_bridge", open_remote)
    first = session.driver()
    session.captures.latest = "c1"
    made[0]._process.kill()
    made[0]._process.wait()
    second = session.driver()  # the next operation gets a new connection …
    try:
        assert second is not first and second.connected() and len(made) == 2
        assert all(c.retired for c in session.captures._items.values())  # … and nothing seen before counts
    finally:
        session.close()

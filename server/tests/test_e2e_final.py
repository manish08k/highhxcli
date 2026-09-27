"""Final end-to-end: CLI → auth → platform → entitlement → gateway → model → agent → tool →
verification → local audit/persistence → server metering/session sync; plus the kill switch.
Runs on SQLite and PostgreSQL."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from helpers import PASSWORD, LiveServer, Upstream, text_reply, tool_reply
from highhx.cli import run
from highhx.storage.database import Database
from highhx_platform.app import create_app
from highhx_platform.config import Settings
from highhx_platform.models import AgentSession, UsageRecord

PY = sys.executable


@pytest.fixture
def platform(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[LiveServer, Upstream, Any]]:
    for name in ("HIGHHX_DATA_DIR", "HIGHHX_CONFIG_DIR", "HIGHHX_CACHE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HIGHHX_NON_INTERACTIVE", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("HIGHHX_TOKEN", raising=False)
    settings.provider_keys = {"anthropic": "sk-ant-test"}
    upstream = Upstream()
    app = create_app(settings, provider_factory=upstream.factory)
    with LiveServer(app) as server:
        monkeypatch.setenv("HIGHHX_API_URL", server.url)
        yield server, upstream, app
    app.state.db.engine.dispose()


def pro_token(server: LiveServer, email: str) -> str:
    status, data = server.json("POST", "/v1/auth/signup", {"email": email, "password": PASSWORD})
    assert status == 201, data
    server.json("POST", "/v1/admin/plan", {"email": email, "plan": "pro"}, {"x-admin-token": "admin-secret"})
    return str(data["access_token"])


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    (root / "app.py").write_text("print('shop')\n")
    return root


def invoke(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    capsys.readouterr()
    code = run(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_to_tool_to_persistence(
    platform: Any, project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    server, upstream, app = platform
    token = pro_token(server, "e2e@example.com")
    monkeypatch.setattr("sys.stdin", io.StringIO(token + "\n"))
    assert invoke(capsys, "login", "--with-token", "--json")[0] == 0

    command = f"{PY} -c \"open('built.txt','w').write('ok')\""
    upstream.responses += [
        tool_reply("run_command", {"command": command}),
        text_reply("Built the project; built.txt contains ok."),
    ]
    code, out, err = invoke(capsys, "agent", "build it", "--json", "--yes", "--mode", "auto-edit", "-C", str(project))
    assert code == 0, err
    result = json.loads(out)
    assert result["ok"] and result["tools"] == [{"name": "run_command", "ok": True}]

    # The tool ran exactly once, through the HighhX engine, and its result was verified.
    assert (project / "built.txt").read_text() == "ok"
    second_request = upstream.requests[1][1]
    assert "succeeded" in second_request.messages[-1].tool_results[0].content
    assert "<untrusted-data" in second_request.messages[-1].tool_results[0].content

    # Local persistence: audit trail + session lifecycle.
    db = Database.open(Path(os.environ["HIGHHX_DATA_DIR"]) / "history.db")
    try:
        audit = db.query("SELECT tool, decision, status, verified FROM audit_log")
        sessions = db.query("SELECT status, turns, account_id FROM agent_sessions")
    finally:
        db.close()
    assert audit == [{"tool": "run_command", "decision": "allowed", "status": "ok", "verified": 1}]
    assert sessions[0]["status"] == "closed" and sessions[0]["turns"] == 1 and sessions[0]["account_id"]

    # Server: metered once per attempt, with distinct idempotency keys; session synced.
    with app.state.db.sessions() as s:
        usage = list(s.scalars(select(UsageRecord).order_by(UsageRecord.created_at)).all())
        remote = s.scalars(select(AgentSession)).one()
    assert [u.status for u in usage] == ["ok", "ok"]
    assert len({u.request_id for u in usage}) == 2
    assert remote.status == "closed" and remote.turns == 1


def test_kill_switch_stops_a_running_agent_and_its_commands(platform: Any, project: Path, tmp_path: Path) -> None:
    server, upstream, _app = platform
    token = pro_token(server, "kill@example.com")
    child_pid = project / "child.pid"
    command = f"{PY} -c \"import os, time; open('child.pid','w').write(str(os.getpid())); time.sleep(120)\""
    upstream.responses += [tool_reply("run_command", {"command": command, "timeout": 300}), text_reply("never reached")]
    env = {**os.environ, "HIGHHX_TOKEN": token}
    agent = subprocess.Popen(
        [PY, "-m", "highhx", "agent", "run a long job", "--yes", "--mode", "auto-edit", "-C", str(project)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not child_pid.exists() and time.monotonic() < deadline:
            assert agent.poll() is None, agent.communicate()
            time.sleep(0.1)
        assert child_pid.exists(), "the long-running command never started"
        pid = int(child_pid.read_text())
        stop = subprocess.run(
            [PY, "-m", "highhx", "agent", "stop", "--all", "--json"],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert stop.returncode == 0, stop.stderr
        assert json.loads(stop.stdout)["stopped"][0]["stopped"] is True
        code = agent.wait(timeout=30)
        assert code == 1
        time.sleep(0.5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)  # the command's process was terminated too
    finally:
        if agent.poll() is None:
            agent.kill()
            agent.wait()

"""End to end: the real `highhx` CLI talking to a real platform server over HTTP + SSE."""

from __future__ import annotations

import io
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from helpers import PASSWORD, Upstream, text_reply, tool_reply
from highhx.cli import run
from highhx.cloud.client import PlatformClient
from highhx.core.errors import AccountError
from highhx_platform.app import create_app
from highhx_platform.config import Settings


@pytest.fixture
def isolated_user_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HIGHHX_DATA_DIR", "HIGHHX_CONFIG_DIR", "HIGHHX_CACHE_DIR"):
        monkeypatch.setenv(name, str(tmp_path / name.lower()))
    monkeypatch.setenv("HIGHHX_NON_INTERACTIVE", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("HIGHHX_TOKEN", raising=False)


@pytest.fixture
def live_platform(
    tmp_path: Path, isolated_user_dirs: None, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[str, Upstream]]:
    upstream = Upstream()
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'platform.db'}",
        public_url="http://127.0.0.1",
        provider_keys={"anthropic": "sk-ant-test"},
        admin_token="admin-secret",  # highhx:allow-secret (test fixture)
        device_poll_interval=0,
    )
    app = create_app(settings, provider_factory=upstream.factory)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "platform did not start"
    url = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HIGHHX_API_URL", url)
    try:
        yield url, upstream
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def cli(
    capsys: pytest.CaptureFixture[str],
    *argv: str,
    stdin: str | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> tuple[int, str, str]:
    capsys.readouterr()
    if stdin is not None and monkeypatch is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = run(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def make_account(url: str, email: str, *, pro: bool) -> str:
    client = PlatformClient(url)
    token = str(client.post("/v1/auth/signup", {"email": email, "password": PASSWORD})["access_token"])
    if pro:
        import urllib.request

        request = urllib.request.Request(
            f"{url}/v1/admin/plan",
            data=json.dumps({"email": email, "plan": "pro"}).encode(),
            headers={"Content-Type": "application/json", "X-Admin-Token": "admin-secret"},
            method="POST",
        )
        urllib.request.urlopen(request).read()
    return token


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "shop"
    root.mkdir()
    (root / "README.md").write_text("# Shop\nA tiny web shop.\n")
    (root / "app.py").write_text("def total(items):\n    return sum(items)\n")
    return root


def test_login_account_and_agent_through_the_platform(
    live_platform: tuple[str, Upstream],
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, upstream = live_platform
    token = make_account(url, "pro@example.com", pro=True)

    code, out, _err = cli(capsys, "login", "--with-token", "--json", stdin=token + "\n", monkeypatch=monkeypatch)
    assert code == 0 and json.loads(out)["plan"] == "pro"

    code, out, _err = cli(capsys, "account", "--json")
    account: dict[str, Any] = json.loads(out)
    assert code == 0 and account["user"]["email"] == "pro@example.com" and "agent" in account["features"]

    upstream.responses += [
        tool_reply("read_file", {"path": "README.md"}),
        text_reply("This is a tiny web shop with a `total` helper."),
    ]
    code, out, err = cli(capsys, "agent", "explain this project", "--json", "-C", str(project))
    assert code == 0, err
    result = json.loads(out)
    assert result["ok"] and result["text"] == "This is a tiny web shop with a `total` helper."
    assert result["tools"] == [{"name": "read_file", "ok": True}]
    # The gateway forwarded the full conversation, including the tool result.
    second = upstream.requests[1][1]
    assert "A tiny web shop." in second.messages[-1].tool_results[0].content
    assert "Project: shop" in second.system and len(second.tools) > 20

    client = PlatformClient(url, token)
    usage = client.get("/v1/usage")
    assert usage["requests"] == 2 and usage["tokens_used"] == 550 + 1200
    sessions = client.get("/v1/agent/sessions")["items"]
    assert len(sessions) == 1 and sessions[0]["turns"] == 1 and sessions[0]["title"] == "explain this project"
    assert sessions[0]["status"] == "closed"

    code, out, _err = cli(capsys, "agent", "sessions", "--json", "-C", str(project))
    assert json.loads(out)["sessions"][0]["title"] == "explain this project"

    code, out, _err = cli(capsys, "logout", "--json")
    assert json.loads(out) == {"signed_out": True}
    with pytest.raises(AccountError, match=r"expired or was revoked"):
        client.get("/v1/me")


def test_free_account_is_told_about_pro(
    live_platform: tuple[str, Upstream],
    project: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, upstream = live_platform
    token = make_account(url, "free@example.com", pro=False)
    monkeypatch.setenv("HIGHHX_TOKEN", token)
    code, _out, err = cli(capsys, "agent", "fix my tests", "-C", str(project))
    assert code == 10
    assert "requires HighhX Pro" in err and "highhx account upgrade" in err
    assert upstream.requests == []
    # Free keeps the whole CLI.
    code, out, _err = cli(capsys, "status", "--json", "-C", str(project))
    assert code == 0 and json.loads(out)["project"]["name"] == "shop"


def test_offline_platform_is_reported_clearly(
    isolated_user_dirs: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], project: Path
) -> None:
    monkeypatch.setenv("HIGHHX_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HIGHHX_TOKEN", "hhx_whatever")
    code, _out, err = cli(capsys, "agent", "hi", "-C", str(project))
    assert code == 1 and "Cannot reach the HighhX platform" in err

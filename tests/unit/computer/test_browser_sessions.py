"""Managed browser sessions: one interface for local and remote browsers — start, heartbeat,
reconnect, stop, cleanup, profile leases; tokens never stored. Real Chrome: HIGHHX_TEST_BROWSER=1."""

from __future__ import annotations

import json
import os
import signal
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.browser import find_browser
from highhx.computer.browser_sessions import BrowserSessionManager
from highhx.computer.profiles import ProfileStore
from highhx.core.errors import IntegrationError, UsageError
from tests.unit.actions.conftest import executor_for  # noqa: F401
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


class FakeBrowser:
    alive: dict[str, bool] = {}

    def __init__(self, state_dir: Path, endpoint: str) -> None:
        self.key = endpoint or str(state_dir)
        self.endpoint = endpoint

    def start(self, *, cancel: Any = None) -> dict[str, Any]:
        if self.endpoint and not FakeBrowser.alive.get(self.key, True):
            raise IntegrationError("unreachable")
        FakeBrowser.alive[self.key] = True
        return {"pid": os.getpid(), "port": 9333}

    def _state(self) -> dict[str, Any] | None:
        return {"pid": os.getpid()} if FakeBrowser.alive.get(self.key) else None

    def stop(self) -> bool:
        FakeBrowser.alive[self.key] = False
        return True


def test_local_sessions_lease_their_profile_and_stop_cleanly(tmp_path: Path) -> None:
    FakeBrowser.alive = {}
    manager = BrowserSessionManager(tmp_path, factory=FakeBrowser)
    ProfileStore(tmp_path).create("work")
    a = manager.start(profile="default")
    b = manager.start(profile="work")
    assert a.kind == b.kind == "local" and a.devtools == "http://127.0.0.1:9333"
    assert {s.id for s in manager.list()} == {a.id, b.id}
    with pytest.raises(UsageError, match="in use by"):
        manager.start(profile="work")  # one session per profile
    assert ProfileStore(tmp_path).info("work").leased_by == b.id
    assert manager.heartbeat(b.id).healthy
    FakeBrowser.alive[str(tmp_path / "profiles" / "work")] = False  # the browser died
    assert not manager.heartbeat(b.id).healthy
    revived = manager.reconnect(b.id)
    assert revived.healthy and revived.reconnects == 1
    manager.stop(b.id)
    assert ProfileStore(tmp_path).info("work").leased_by == "" and b.id not in {s.id for s in manager.list()}
    with pytest.raises(UsageError, match="No browser session"):
        manager.stop(b.id)
    assert oct((tmp_path / "browser-sessions.json").stat().st_mode & 0o777) == "0o600"


def test_remote_sessions_never_store_the_token_and_cleanup_forgets_dead_ones(tmp_path: Path) -> None:
    FakeBrowser.alive = {}
    manager = BrowserSessionManager(tmp_path, factory=FakeBrowser)
    remote = manager.start(endpoint="wss://browser.example.com/devtools/browser/1?token=rb-TOKEN-1")
    assert remote.kind == "remote" and remote.devtools == "wss://browser.example.com/devtools/browser/1"
    assert "rb-TOKEN-1" not in (tmp_path / "browser-sessions.json").read_text()
    assert manager.heartbeat(remote.id).healthy  # this process still knows the full endpoint
    sessions = json.loads((tmp_path / "browser-sessions.json").read_text())
    sessions[0]["last_heartbeat"] = time.time() - 10 * 3600  # idle for hours
    (tmp_path / "browser-sessions.json").write_text(json.dumps(sessions))
    assert manager.cleanup() == [remote.id] and manager.list() == []


def test_session_actions_go_through_the_executor(agent_project, executor_for, tmp_path, monkeypatch) -> None:  # noqa: F811
    from highhx.computer import browser_sessions

    FakeBrowser.alive = {}
    monkeypatch.setattr("highhx.utils.paths.user_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        browser_sessions,
        "_default_factory",
        lambda state_dir, endpoint, headless=None: FakeBrowser(state_dir, endpoint),
    )
    executor, _ = executor_for(agent_project)
    started = executor.run("browser.session_start", {"endpoint": "http://10.0.0.5:9222/json?token=sk-REMOTE-9"})
    assert started.ok and started.output["kind"] == "remote"
    assert (
        executor.plan("browser.session_start", {"endpoint": "http://10.0.0.5:9222"}).decision.risk == 2
    )  # another computer: medium
    listed = executor.run("browser.sessions", {}).output["sessions"]
    assert [s["id"] for s in listed] == [started.output["id"]]
    assert executor.run("browser.session_stop", {"session": started.output["id"]}).ok
    from highhx.safety.audit import AuditLog

    rows = json.dumps(
        [e.__dict__ for e in AuditLog(executor.app.db, executor.app.redactor).list(limit=20)], default=str
    )
    assert "sk-REMOTE-9" not in rows


@pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)
def test_sessions_in_real_chrome(tmp_path: Path) -> None:
    from highhx.computer.browser import ChromeBrowser

    manager = BrowserSessionManager(tmp_path, headless=True)
    ProfileStore(tmp_path).create("second")
    first = manager.start(profile="default")
    second = manager.start(profile="second")
    try:
        assert first.browser_id != second.browser_id  # two browsers, side by side
        assert manager.heartbeat(first.id).healthy and manager.heartbeat(second.id).healthy
        assert manager.screenshot(first.id)[:8] == b"\x89PNG\r\n\x1a\n"
        os.kill(int(second.browser_id), signal.SIGKILL)  # the browser crashes
        for _ in range(50):
            if not manager.heartbeat(second.id).healthy:
                break
            time.sleep(0.1)
        assert not manager.heartbeat(second.id).healthy
        revived = manager.reconnect(second.id)
        assert revived.healthy and revived.browser_id != second.browser_id and revived.reconnects == 1

        # a browser HighhX did not start, through the same interface
        other = ChromeBrowser(tmp_path / "elsewhere", headless=True)
        port = other.start()["port"]
        remote = manager.start(endpoint=f"http://127.0.0.1:{port}?token=local-TOKEN")
        assert remote.kind == "remote" and "local-TOKEN" not in (tmp_path / "browser-sessions.json").read_text()
        assert manager.heartbeat(remote.id).healthy
        manager.stop(remote.id)
        assert other.running  # stopping a remote session only disconnects
        other.stop()
    finally:
        for session in manager.list():
            manager.stop(session.id)
    assert manager.list() == []


def test_a_remote_http_endpoint_with_a_token_keeps_the_token_in_the_query(tmp_path: Path) -> None:
    """Regression: the version URL was built by appending to the whole endpoint, so
    ``http://host:9222?token=…`` became ``…?token=…/json/version`` and never answered."""
    import http.server
    import threading

    from highhx.computer.browser import RemoteBrowser

    seen: list[str] = []

    class DevTools(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            body = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/browser/x"}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DevTools)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_address[1]}?token=tk-1"
        assert RemoteBrowser(tmp_path, endpoint)._ws_url() == "ws://127.0.0.1:1/devtools/browser/x"
        assert RemoteBrowser(tmp_path, f"http://127.0.0.1:{server.server_address[1]}/")._ws_url()
        assert seen == ["/json/version?token=tk-1", "/json/version"]
    finally:
        server.shutdown()
        server.server_close()

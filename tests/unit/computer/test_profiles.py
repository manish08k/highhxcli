"""Browser profiles: isolated, persistent, owner-only, never read; leases; actions through the
executor (deleting and importing are always asked). Real-Chrome part opt-in: HIGHHX_TEST_BROWSER=1."""

from __future__ import annotations

import http.server
import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.browser import find_browser
from highhx.computer.profiles import DEFAULT, ProfileStore
from highhx.core.errors import UsageError
from tests.unit.actions.conftest import executor_for  # noqa: F401
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


def test_profiles_are_created_listed_isolated_and_deleted(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    assert [p.name for p in store.list()] == [DEFAULT]
    info = store.create("work")
    assert info.name == "work" and Path(info.path) == tmp_path / "profiles" / "work"
    assert stat.S_IMODE((tmp_path / "profiles" / "work").stat().st_mode) == 0o700
    assert store.state_dir(DEFAULT) == tmp_path  # the original profile keeps its place
    assert [p.name for p in store.list()] == [DEFAULT, "work"]
    with pytest.raises(UsageError, match="already exists"):
        store.create("work")
    for bad in ("../etc", "Work", "a b", "", "x" * 41):
        with pytest.raises(UsageError, match="Invalid profile name"):
            store.create(bad)
    with pytest.raises(UsageError, match="cannot be deleted"):
        store.delete(DEFAULT)
    store.delete("work")
    assert not store.exists("work")


def test_a_running_or_leased_profile_is_not_deleted_and_stale_leases_break(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path)
    store.create("work")
    (store.state_dir("work") / "browser.json").write_text(json.dumps({"pid": os.getpid()}))
    with pytest.raises(UsageError, match="running"):
        store.delete("work")
    (store.state_dir("work") / "browser.json").unlink()
    store.lease("work", "sess_a")
    store.lease("work", "sess_a")  # the holder may renew
    assert store.info("work").leased_by == "sess_a"
    with pytest.raises(UsageError, match="in use by sess_a"):
        store.lease("work", "sess_b")
    with pytest.raises(UsageError, match="in use"):
        store.delete("work")
    # a lease whose process exited is stale
    dead = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, check=False
    ).stdout.strip()
    (store.state_dir("work") / "profile.lease").write_text(json.dumps({"owner": "sess_old", "pid": int(dead)}))
    assert store.info("work").leased_by == ""
    store.lease("work", "sess_b")
    store.release("work", "sess_b")
    assert store.info("work").leased_by == ""


def test_import_copies_without_locks_or_caches(tmp_path: Path) -> None:
    source = tmp_path / "chrome-data"
    (source / "Default" / "Cache").mkdir(parents=True)
    (source / "Default" / "Cookies").write_bytes(b"opaque")
    (source / "Default" / "Cache" / "blob").write_bytes(b"x" * 10)
    (source / "DevToolsActivePort").write_text("9222\n")
    store = ProfileStore(tmp_path / "state")
    info = store.import_from("moved", source)
    copied = Path(info.path) / "browser-profile"
    assert (copied / "Default" / "Cookies").read_bytes() == b"opaque"
    assert not (copied / "Default" / "Cache").exists() and not (copied / "DevToolsActivePort").exists()
    (source / "SingletonLock").symlink_to("host-1")
    with pytest.raises(UsageError, match="in use by a running browser"):
        store.import_from("again", source)


def test_profile_actions_go_through_the_executor(agent_project: Path, make_app, tmp_path: Path) -> None:  # noqa: F811
    from highhx.actions.executor import ActionExecutor
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.unit.agent.conftest import RecordingUI

    app = make_app(agent_project)
    ui = RecordingUI()
    gate = ActionGate(app.engine, ui, source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "computer")
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    assert executor.run("browser.profile_create", {"name": "shop"}).ok
    names = [p["name"] for p in executor.run("browser.profiles", {}).output["profiles"]]
    assert names == [DEFAULT, "shop"]
    assert executor.plan("browser.profile_delete", {"name": "shop"}).decision.risk == 3
    ui.action_answers = [False]
    assert executor.run("browser.profile_delete", {"name": "shop"}).status == "denied"
    assert ProfileStore(tmp_path / "computer").exists("shop")  # declined: kept
    assert executor.run("browser.profile_delete", {"name": "shop"}).ok
    assert executor.plan("browser.profile_import", {"name": "x", "source": "/tmp"}).decision.risk == 3


def test_a_session_runs_on_the_named_profile(tmp_path: Path, monkeypatch) -> None:
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor

    ProfileStore(tmp_path).create("work")
    session = ComputerSession(object(), actor=Actor.USER, state_dir=tmp_path, profile="work")  # type: ignore[arg-type]
    assert session.state_dir == tmp_path / "profiles" / "work"
    monkeypatch.setenv("HIGHHX_BROWSER_PROFILE", "work")
    assert ComputerSession(object(), actor=Actor.USER, state_dir=tmp_path).state_dir == tmp_path / "profiles" / "work"  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="No browser profile named 'nope'"):
        ComputerSession(object(), actor=Actor.USER, state_dir=tmp_path, profile="nope")  # type: ignore[arg-type]


class CookieSite(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = f"<!doctype html><title>seen</title><p id=c>{self.headers.get('Cookie') or 'none'}</p>"
        self.send_response(200)
        if self.path == "/login":
            self.send_header("Set-Cookie", "session=sess-PROFILE-SECRET; Max-Age=3600; Path=/")
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args: Any) -> None:
        pass


@pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)
def test_profiles_keep_sessions_apart_and_across_restarts_in_real_chrome(tmp_path: Path) -> None:
    from highhx.computer.browser import ChromeBrowser

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CookieSite)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    store = ProfileStore(tmp_path)
    store.create("a")
    store.create("b")
    a = ChromeBrowser(store.state_dir("a"), headless=True)
    b = ChromeBrowser(store.state_dir("b"), headless=True)
    try:
        a.navigate(f"{base}/login")
        a.navigate(f"{base}/check")
        assert "sess-PROFILE-SECRET" in (a.observe().text or "")
        b.navigate(f"{base}/check")
        assert "none" in (b.observe().text or "")  # profile b never sees a's cookie
        assert store.info("a").running and store.info("b").running
        assert a.stop()
        a2 = ChromeBrowser(store.state_dir("a"), headless=True)  # the session survives a restart
        a2.navigate(f"{base}/check")
        assert "sess-PROFILE-SECRET" in (a2.observe().text or "")
        assert a2.stop()
        listing = json.dumps([p.to_dict() for p in store.list()])
        assert "sess-PROFILE-SECRET" not in listing  # contents are never read
    finally:
        b.stop()
        server.shutdown()
        server.server_close()

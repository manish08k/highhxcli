"""The web console: loopback-only, token-protected (host, CSRF, CSP), tasks through the one agent
loop and executor, approvals answered from the page, history, events, controls, live view.
Real-Chrome tests (live frames, the page itself rendering untrusted text safely): HIGHHX_TEST_BROWSER=1."""

from __future__ import annotations

import http.client
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from highhx.computer.browser import find_browser
from highhx.ui.web import WebConsole
from tests.unit.agent.conftest import agent_project, make_app  # noqa: F401


def call(
    console: WebConsole, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, Any, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=10)
    data = json.dumps(body).encode() if body is not None else None
    conn.request(method, path, body=data, headers={"Host": f"127.0.0.1:{console.port}", **(headers or {})})
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        parsed: Any = json.loads(raw) if raw else None
    except ValueError:
        parsed = raw.decode("utf-8", "replace")
    return response.status, parsed, dict(response.getheaders())


@pytest.fixture
def console(agent_project: Path, make_app) -> Any:  # noqa: F811
    app = make_app(agent_project)
    web = WebConsole(app, headless=True)
    web.start()
    yield web
    web.close()


def authed(console: WebConsole) -> dict[str, str]:
    return {"X-HighhX-Token": console.token, "Content-Type": "application/json"}


def wait_for(fn: Any, timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("condition not met in time")


def test_every_request_is_guarded(console: WebConsole) -> None:
    assert call(console, "GET", "/api/state")[0] == 401
    assert call(console, "GET", "/api/state?token=wrong")[0] == 401
    assert (
        call(console, "GET", f"/api/state?token={console.token}", headers={"Host": "evil.example:80"})[0] == 421
    )  # DNS rebinding
    # state changes need the header (a query token is not enough: CSRF) and a same-origin Origin
    assert call(console, "POST", f"/api/tasks?token={console.token}", {"goal": "x"})[0] == 401
    status, body, _ = call(
        console, "POST", "/api/tasks", {"goal": "x"}, {**authed(console), "Origin": "https://evil.example"}
    )
    assert status == 403 and "cross-origin" in body["error"]
    status, page, headers = call(console, "GET", f"/?token={console.token}")
    assert status == 200 and "<title>HighhX console</title>" in page
    policy = headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "script-src 'nonce-" in policy and "frame-ancestors 'none'" in policy
    assert headers["Referrer-Policy"] == "no-referrer" and headers["X-Content-Type-Options"] == "nosniff"
    assert console.server.server_address[0] == "127.0.0.1"


def test_a_web_task_and_its_approval(console: WebConsole, agent_project: Path) -> None:  # noqa: F811
    steps = [{"action": "filesystem.write", "parameters": {"path": "from-web.txt", "content": "hi\n"}}]
    status, task, _ = call(
        console, "POST", "/api/tasks", {"goal": "write a file", "surface": "none", "steps": steps}, authed(console)
    )
    assert status == 201 and task["status"] in ("starting", "queued", "running")
    pending = wait_for(lambda: call(console, "GET", f"/api/state?token={console.token}")[1]["approvals"])
    assert "from-web.txt" in pending[0]["action"]
    status, decided, _ = call(
        console, "POST", f"/api/approvals/{pending[0]['id']}", {"decision": "approve"}, authed(console)
    )
    assert status == 200 and decided["status"] == "approved" and decided["decided_by"] == "web console"
    done = wait_for(
        lambda: next(
            (
                t
                for t in call(console, "GET", f"/api/state?token={console.token}")[1]["tasks"]
                if t["status"] not in ("starting", "queued", "running")
            ),
            None,
        )
    )
    assert done["status"] == "completed" and (agent_project / "from-web.txt").read_text() == "hi\n"
    history = call(console, "GET", f"/api/history?token={console.token}")[1]["tasks"]
    assert history and history[0]["task"] == "write a file"
    detail = call(console, "GET", f"/api/history/{history[0]['id']}?token={console.token}")[1]
    assert detail["trajectory"]["steps"][0]["action"]["action_type"] == "filesystem.write"
    events = call(console, "GET", f"/api/events?token={console.token}&after=0")[1]["events"]
    names = [e["event"] for e in events]
    assert "task.started" in names and "approval.required" in names and "action.completed" in names


def test_rejected_and_invalid_requests(console: WebConsole, agent_project: Path) -> None:  # noqa: F811
    steps = [{"action": "filesystem.write", "parameters": {"path": "nope.txt", "content": "x\n"}}]
    call(console, "POST", "/api/tasks", {"goal": "write", "surface": "none", "steps": steps}, authed(console))
    pending = wait_for(lambda: call(console, "GET", f"/api/state?token={console.token}")[1]["approvals"])
    call(console, "POST", f"/api/approvals/{pending[0]['id']}", {"decision": "reject"}, authed(console))
    wait_for(
        lambda: all(
            t["status"] not in ("starting", "queued", "running")
            for t in call(console, "GET", f"/api/state?token={console.token}")[1]["tasks"]
        )
    )
    assert not (agent_project / "nope.txt").exists()
    assert call(console, "POST", "/api/tasks", {"goal": ""}, authed(console))[0] == 400
    assert call(console, "POST", "/api/tasks", {"goal": "x", "surface": "mainframe"}, authed(console))[0] == 400
    assert call(console, "POST", "/api/approvals/apr_missing", {"decision": "approve"}, authed(console))[0] == 404
    assert call(console, "POST", "/api/tasks/web_missing/pause", {}, authed(console))[0] == 404


def test_pause_resume_and_cancel(console: WebConsole, agent_project: Path) -> None:  # noqa: F811
    steps = [{"action": "wait"}] + [
        {"action": "filesystem.write", "parameters": {"path": f"p{i}.txt", "content": "x\n"}} for i in range(3)
    ]
    _, task, _ = call(
        console, "POST", "/api/tasks", {"goal": "slow", "surface": "none", "steps": steps}, authed(console)
    )
    call(console, "POST", f"/api/tasks/{task['id']}/pause", {}, authed(console))
    assert call(console, "GET", f"/api/state?token={console.token}")[1]["tasks"][0]["status"] in (
        "paused",
        "queued",
        "starting",
    )
    call(console, "POST", f"/api/tasks/{task['id']}/cancel", {}, authed(console))
    final = wait_for(
        lambda: next(
            (
                t
                for t in call(console, "GET", f"/api/state?token={console.token}")[1]["tasks"]
                if t["status"] not in ("starting", "queued", "running", "paused")
            ),
            None,
        )
    )
    assert final["status"] in ("cancelled", "interrupted") and not (agent_project / "p2.txt").exists()


LIVE = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)


@LIVE
def test_the_live_view_streams_real_frames(console: WebConsole) -> None:
    session = console.session()
    session.browser.navigate("data:text/html,<h1 style='font-size:80px'>live</h1>")
    conn = http.client.HTTPConnection("127.0.0.1", console.port, timeout=20)
    conn.request(
        "GET", f"/api/frame?source=browser&token={console.token}", headers={"Host": f"127.0.0.1:{console.port}"}
    )
    response = conn.getresponse()
    data = response.read()
    assert response.status == 200 and response.getheader("Content-Type") == "image/jpeg" and data[:2] == b"\xff\xd8"
    stats = call(console, "GET", f"/api/state?token={console.token}")[1]["streams"]["browser"]
    assert stats["frames"] >= 1 and stats["bytes"] > 0
    session.browser.stop()


@LIVE
def test_the_page_shows_untrusted_text_as_text_in_real_chrome(console: WebConsole, tmp_path: Path) -> None:
    from highhx.computer.browser import ChromeBrowser

    console.app.ctx.events.emit(
        "action.completed", action="<img src=x onerror=\"document.title='pwned'\">", summary="x"
    )
    viewer = ChromeBrowser(tmp_path / "viewer", headless=True)
    try:
        viewer.navigate(console.url)
        wait_for(
            lambda: (
                "action.completed"
                in (viewer.evaluate("document.getElementById('activity').textContent", retry_safe=True) or "")
            )
        )
        assert viewer.evaluate("document.querySelectorAll('#activity img').length", retry_safe=True) == 0
        assert viewer.evaluate("document.title", retry_safe=True) == "HighhX console"
        text = viewer.evaluate("document.getElementById('activity').textContent", retry_safe=True)
        assert '<img src=x onerror="document.title=' in text  # shown, never parsed
        assert "capabilities available here" in viewer.evaluate(
            "document.getElementById('caps').textContent", retry_safe=True
        )
    finally:
        viewer.stop()

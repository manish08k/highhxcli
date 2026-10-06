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


def tasks_of(console: WebConsole) -> list[dict[str, Any]]:
    return list(call(console, "GET", f"/api/state?token={console.token}")[1]["tasks"])


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


def test_idempotent_honest_states(console: WebConsole, agent_project: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    steps = [{"action": "filesystem.write", "parameters": {"path": "once.txt", "content": "x\n"}}]
    body = {"goal": "once", "surface": "none", "steps": steps, "request_id": "r-1"}
    first = call(console, "POST", "/api/tasks", body, authed(console))
    again = call(console, "POST", "/api/tasks", body, authed(console))  # a double click / a retried request
    assert (first[0], again[0]) == (201, 200) and first[1]["id"] == again[1]["id"] and len(console.tasks) == 1
    # waiting for an answer is what the state says, not "running"
    wait_for(lambda: call(console, "GET", f"/api/state?token={console.token}")[1]["approvals"])
    assert tasks_of(console)[0]["status"] == "waiting"
    # cancelling it ends it now (it used to wait for the approval's deadline) and withdraws the approval
    call(console, "POST", f"/api/tasks/{first[1]['id']}/cancel", {}, authed(console))
    wait_for(lambda: tasks_of(console)[0]["status"] not in ("starting", "queued", "running", "waiting"), timeout=5)
    assert not (agent_project / "once.txt").exists()
    assert [a.status for a in console.approvals.all()] == ["cancelled"]
    # controls on an ended task are refused with the reason, unknown ones are a bad request
    status, answer, _ = call(console, "POST", f"/api/tasks/{first[1]['id']}/pause", {}, authed(console))
    assert status == 409 and "already ended" in answer["error"]
    assert call(console, "POST", f"/api/tasks/{first[1]['id']}/explode", {}, authed(console))[0] == 400
    # an unexpected failure is an answer the page can show, never a dropped connection
    monkeypatch.setattr(console, "history", lambda limit=50: 1 / 0)
    status, answer, _ = call(console, "GET", f"/api/history?token={console.token}")
    assert status == 500 and "could not read that" in answer["error"]


def test_ended_tasks_leave_no_token_on_the_console(console: WebConsole) -> None:
    # Regression: each task's token was a child of the console's and was never let go, so a
    # long-running console held every task it ever ran (and whatever its callbacks referenced).
    root = console.app.ctx.cancel
    held = lambda: [t.id for t in console.tasks.values() if t.cancel.cancel in root._callbacks]  # noqa: E731
    for goal in ("one", "two"):  # tasks that run to the end
        call(
            console,
            "POST",
            "/api/tasks",
            {"goal": goal, "surface": "none", "steps": [{"action": "wait"}]},
            authed(console),
        )
    wait_for(lambda: len(console.tasks) == 2 and all(t.ended for t in console.tasks.values()))
    assert all(t.status == "completed" for t in console.tasks.values())
    # one waits for an answer, another is cancelled while queued behind it (it never starts)
    steps = [{"action": "filesystem.write", "parameters": {"path": "w.txt", "content": "x\n"}}]
    call(console, "POST", "/api/tasks", {"goal": "blocker", "surface": "none", "steps": steps}, authed(console))
    pending = wait_for(lambda: call(console, "GET", f"/api/state?token={console.token}")[1]["approvals"])
    _, queued, _ = call(
        console, "POST", "/api/tasks", {"goal": "queued", "surface": "none", "steps": steps}, authed(console)
    )
    call(console, "POST", f"/api/tasks/{queued['id']}/cancel", {}, authed(console))
    call(console, "POST", f"/api/approvals/{pending[0]['id']}", {"decision": "reject"}, authed(console))
    wait_for(lambda: len(console.tasks) == 4 and all(t.ended for t in console.tasks.values()))
    assert console.tasks[queued["id"]].summary == "cancelled before it started"
    assert held() == []


def test_the_person_takes_the_computer_and_hands_it_back(console: WebConsole) -> None:
    status, answer, _ = call(console, "POST", "/api/computer/release", {}, authed(console))
    assert status == 409 and "not taken over" in answer["error"]
    status, answer, _ = call(console, "POST", "/api/computer/take", {}, authed(console))
    assert status == 200 and answer == {"taken_over": True, "by": "the person at the web console"}
    assert call(console, "GET", f"/api/state?token={console.token}")[1]["computer"]["taken_over"] is True
    assert console.session().taken_over  # the executor refuses changes to the computer meanwhile
    assert call(console, "POST", "/api/computer/release", {}, authed(console))[1]["taken_over"] is False
    names = [e["event"] for e in call(console, "GET", f"/api/events?token={console.token}&after=0")[1]["events"]]
    assert "computer.taken_over" in names and "computer.released" in names
    assert call(console, "POST", "/api/computer/take", {}, {"Content-Type": "application/json"})[0] == 401  # guarded


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


# ---------------------------------------- the page itself, driven like a person in real Chrome
class Page:
    """The console page in a real browser: real mouse clicks and keystrokes (DevTools input)."""

    def __init__(self, console: WebConsole, folder: Path) -> None:
        from highhx.computer.browser import ChromeBrowser

        self.viewer = ChromeBrowser(folder, headless=True)
        self.viewer.navigate(console.url)
        wait_for(lambda: self.js("document.getElementById('status').textContent") not in ("", "connecting…"))

    def js(self, expression: str) -> Any:
        return self.viewer.evaluate(expression, retry_safe=True)

    def click(self, selector: str, count: int = 1) -> None:
        box = self.js(
            f"(() => {{ const r = document.querySelector({json.dumps(selector)}).getBoundingClientRect();"
            " return [r.x + r.width / 2, r.y + r.height / 2]; })()"
        )
        self.viewer.pointer("click", box[0], box[1], count=count)

    def type(self, selector: str, text: str) -> None:
        self.click(selector)
        self.viewer.insert_text(text)

    def status(self) -> str:
        return str(self.js("document.getElementById('status').textContent"))

    def close(self) -> None:
        self.viewer.stop()


@LIVE
def test_page_double_submits_make_one_task(console: WebConsole, tmp_path: Path) -> None:
    page = Page(console, tmp_path / "viewer")
    try:
        page.js(
            "document.getElementById('steps').value = JSON.stringify([{action: 'wait'}]);"
            "document.getElementById('surface').value = 'none'"
        )
        page.type("#goal", "first")
        page.click("#run", count=2)  # a double click
        wait_for(lambda: tasks_of(console))
        time.sleep(1.0)
        assert [t["goal"] for t in tasks_of(console)] == ["first"]
        page.type("#goal", "second")
        page.viewer.press("enter")  # Enter runs it, a second Enter while it is starting does nothing
        page.viewer.press("enter")
        wait_for(lambda: len(tasks_of(console)) == 2)
        time.sleep(1.0)
        assert sorted(t["goal"] for t in tasks_of(console)) == ["first", "second"]
        assert page.js("document.getElementById('formerror').textContent") in ("", "say what HighhX should do")
    finally:
        page.close()


@LIVE
def test_page_approvals_by_click(console: WebConsole, tmp_path: Path, agent_project: Path) -> None:  # noqa: F811
    page = Page(console, tmp_path / "viewer")
    (agent_project / "build").mkdir()
    (agent_project / "keep").mkdir()
    try:
        steps = [{"action": "shell.run", "parameters": {"command": f"rm -rf {agent_project / 'build'}"}}]
        call(console, "POST", "/api/tasks", {"goal": "clean", "surface": "none", "steps": steps}, authed(console))
        wait_for(lambda: page.js("document.querySelectorAll('#approvals input').length") == 1)
        assert page.status() == "waiting for approval (1)"
        page.type("#approvals input", "appr")
        time.sleep(3.5)  # two state polls: the row, its half-typed word and the focus stay
        assert page.js("document.querySelector('#approvals input').value") == "appr"
        assert page.js("document.activeElement === document.querySelector('#approvals input')")
        # an invalid modification is explained in place, never in a blocking dialog
        page.click("#approvals li button:nth-of-type(4)")  # Modify…
        page.js("document.querySelector('#approvals textarea').value = '{not json'")
        page.click("#approvals .editor button")
        assert "not valid JSON" in page.js("document.querySelector('#approvals .err').textContent")
        page.click("#approvals input")
        page.viewer.press("end")
        page.viewer.insert_text("ove")
        page.viewer.press("enter")  # Enter approves (the typed word is checked by the console)
        wait_for(lambda: tasks_of(console)[0]["status"] == "completed")
        assert not (agent_project / "build").exists()
        wait_for(lambda: page.js("document.activeElement && document.activeElement.id") == "goal")  # focus moved on
        wait_for(lambda: page.status() == "idle")
        # a rejection by click: the action never runs
        steps = [{"action": "shell.run", "parameters": {"command": f"rm -rf {agent_project / 'keep'}"}}]
        call(console, "POST", "/api/tasks", {"goal": "nope", "surface": "none", "steps": steps}, authed(console))
        wait_for(lambda: page.js("document.querySelectorAll('#approvals li button').length") >= 3)
        page.click("#approvals li button:nth-of-type(2)", count=2)  # Reject, double-clicked
        wait_for(lambda: tasks_of(console)[0]["status"] not in ("starting", "queued", "running", "waiting"))
        assert tasks_of(console)[0]["status"] == "failed" and (agent_project / "keep").exists()
        assert [a.status for a in console.approvals.all()].count("rejected") == 1
    finally:
        page.close()


@LIVE
def test_page_cancel_disconnect_and_one_live_loop(console: WebConsole, tmp_path: Path, agent_project: Path) -> None:  # noqa: F811
    from http.server import ThreadingHTTPServer

    page = Page(console, tmp_path / "viewer")
    try:
        steps = [{"action": "filesystem.write", "parameters": {"path": "c.txt", "content": "x\n"}}]
        call(console, "POST", "/api/tasks", {"goal": "to cancel", "surface": "none", "steps": steps}, authed(console))
        wait_for(
            lambda: page.js("[...document.querySelectorAll('#tasks button')].some(b => b.textContent === 'Cancel')")
        )
        page.js(
            "window.cancelButton = [...document.querySelectorAll('#tasks button')].find(b => b.textContent === 'Cancel');"
            "window.cancelButton.id = 'cancel-1'"
        )
        page.click("#cancel-1")
        wait_for(lambda: tasks_of(console)[0]["status"] not in ("starting", "queued", "running", "waiting"))
        assert tasks_of(console)[0]["status"] in ("cancelled", "interrupted", "failed")
        assert not (agent_project / "c.txt").exists()
        # the console goes away: the page says so, then recovers when it is back
        handler = console.server.RequestHandlerClass
        console.server.shutdown()
        console.server.server_close()
        wait_for(lambda: page.status().startswith("disconnected"), timeout=8)
        console.server = ThreadingHTTPServer(("127.0.0.1", console.port), handler)
        console.server.daemon_threads = True
        console.start()
        wait_for(lambda: page.status() == "idle", timeout=8)
        # rapid Start/Stop of the live view: at most one frame request in flight
        page.js(
            "window.inflight = 0; window.peak = 0; const f = window.fetch; window.fetch = async (...a) => {"
            " const frame = String(a[0]).includes('/api/frame'); if (frame) { inflight++; peak = Math.max(peak, inflight); }"
            " try { return await f(...a); } finally { if (frame) inflight--; } }"
        )
        for _ in range(3):
            page.click("#live")
            page.click("#live")
        page.click("#live")  # finally on
        time.sleep(3)
        assert page.js("window.peak") == 1
        page.click("#live")
    finally:
        page.close()
        if console._session is not None and console._session._browser is not None:
            console._session._browser.stop()


@LIVE
def test_page_take_over_and_release_by_click(console: WebConsole, tmp_path: Path) -> None:
    page = Page(console, tmp_path / "viewer")
    try:
        wait_for(lambda: page.status() == "idle")
        page.click("#takeover")
        wait_for(lambda: page.status() == "you have control — HighhX waits")
        assert page.js("document.getElementById('takeover').textContent") == "Release" and console.session().taken_over
        page.click("#takeover")
        wait_for(lambda: page.status() == "idle")
        assert not console.session().taken_over
    finally:
        page.close()

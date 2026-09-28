"""The browser lifecycle and recovery policy, against a scripted Chrome.

``FakeChrome`` speaks the browser-level DevTools protocol HighhX uses — flat sessions per
tab, target events, downloads — and can fail in every way the real one does: a connection
that drops before or after a command, a tab closed or replaced under HighhX, a renderer
crash, a stalled load, a blocking dialog, a popup, a browser that exits. Each failure is
exact and repeatable. The real browser is exercised by ``test_live_browser.py``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from highhx.computer import browser as browser_module
from highhx.computer import cdp as cdp_module
from highhx.computer.browser import (
    BrowserState,
    ChromeBrowser,
    _network_hint,
    arrived,
    is_sign_in_window,
)
from highhx.computer.cdp import (
    BrowserDisconnectedError,
    BrowserTimeoutError,
    CDPConnection,
    PageCrashedError,
    TargetClosedError,
    dialog_answer,
)
from highhx.computer.runtime import _same_page
from highhx.computer.tabs import TabRegistry, same_document
from highhx.computer.websocket import WebSocket, WebSocketClosed, WebSocketTimeout
from highhx.core.errors import IntegrationError, NotFoundError, OperationCancelledError, OutcomeUnknownError
from highhx.execution.cancellation import CancellationToken


# ------------------------------------------------------------------ the fake
class FakeChrome:
    """Browser-wide state shared by every connection (tabs outlive a dropped connection)."""

    def __init__(self) -> None:
        self.tabs: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, str] = {}
        """session id → tab id"""
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        """(tab or "browser", method, params) for every command that reached the browser."""
        self.connections = 0
        self.alive = True
        self.pid = 4242
        self.next_id = 1
        self.socket: FakeSocket | None = None
        # failure scripts
        self.drop_before: deque[str] = deque()
        """Methods whose next send fails (never delivered)."""
        self.drop_after: deque[tuple[str, bool]] = deque()
        """(method, applied): the connection dies after the command arrived (applied or not)."""
        self.crash_on: deque[str] = deque()
        self.close_tab_on: deque[str] = deque()
        """The tab is closed (by the person) while this method runs in it."""
        self.stalled: set[str] = set()
        """Tabs stuck on a load that never commits: they answer nothing but Page.stopLoading."""
        self.stall_navigation = False
        self.nav_error = ""
        self.redirect: dict[str, str] = {}
        self.dialog = ""
        self.on_click: Any = None
        self.click_href = ""
        self.download_on: dict[str, str] = {}
        """URL → suggested filename: navigating there downloads a file."""
        self.new_tab("about:blank")

    def new_tab(self, url: str = "about:blank", opener: str = "") -> str:
        tab = f"t{self.next_id}"
        self.next_id += 1
        self.tabs[tab] = {"url": url, "title": url, "opener": opener, "history": [url], "index": 0}
        if self.socket is not None:
            self.socket.event("Target.targetCreated", targetInfo=self.info(tab))
        return tab

    def close_tab(self, tab: str) -> None:
        self.tabs.pop(tab, None)
        for session, owner in list(self.sessions.items()):
            if owner == tab:
                del self.sessions[session]
                if self.socket is not None:
                    self.socket.event("Target.detachedFromTarget", sessionId=session, targetId=tab)
        if self.socket is not None:
            self.socket.event("Target.targetDestroyed", targetId=tab)

    def info(self, tab: str) -> dict[str, Any]:
        data = self.tabs[tab]
        info = {"targetId": tab, "type": "page", "url": data["url"], "title": data["title"], "attached": False}
        if data["opener"]:
            info["openerId"] = data["opener"]
        return info

    def kill(self) -> None:
        """The browser process exits: every connection and tab is gone."""
        self.alive = False
        if self.socket is not None:
            self.socket.closed = True
        self.tabs.clear()
        self.sessions.clear()

    def restart(self) -> None:
        self.alive = True
        self.pid += 1
        self.new_tab("about:blank")


class FakeSocket:
    """Stands in for the WebSocket; answers like Chrome's browser endpoint."""

    chrome: FakeChrome

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        if not self.chrome.alive:
            raise ConnectionRefusedError("connection refused")
        self.closed = False
        self.inbox: deque[str] = deque()
        self.chrome.connections += 1
        self.chrome.socket = self
        self.sock = self

    # --- the WebSocket API
    def send(self, text: str) -> None:
        if self.closed:
            raise WebSocketClosed("closed")
        message = json.loads(text)
        method, session = message["method"], str(message.get("sessionId") or "")
        chrome = self.chrome
        if chrome.drop_before and chrome.drop_before[0] == method:
            chrome.drop_before.popleft()
            self.closed = True
            raise WebSocketClosed("The DevTools connection was lost while sending.")
        tab = chrome.sessions.get(session, "") if session else "browser"
        chrome.sent.append((tab, method, message.get("params") or {}))
        if session and session not in chrome.sessions:
            self.reply(message, error="Session with given id not found.")
            return
        if chrome.drop_after and chrome.drop_after[0][0] == method:
            _, applied = chrome.drop_after.popleft()
            if applied:
                self.apply(message, tab)
            self.inbox.clear()
            self.closed = True
            return
        if method == "Page.stopLoading":
            chrome.stalled.discard(tab)
        elif tab in chrome.stalled or (method == "Page.navigate" and chrome.stall_navigation):
            chrome.stall_navigation = False
            chrome.stalled.add(tab)
            return  # no answer will come
        if chrome.crash_on and chrome.crash_on[0] == method and session:
            chrome.crash_on.popleft()
            self.event("Inspector.targetCrashed", session=session)
            self.event("Target.targetCrashed", targetId=tab)
            return
        if chrome.close_tab_on and chrome.close_tab_on[0] == method and session:
            chrome.close_tab_on.popleft()
            chrome.close_tab(tab)
            return
        result = self.apply(message, tab)
        if result is not None:
            self.reply(message, result=result)

    def recv(self, *, cancel: Any = None, poll: float = 0.2, deadline: float | None = None) -> str:
        """Like the real one: the next message, or closed / cancelled / timed out."""
        while True:
            if self.inbox:
                return self.inbox.popleft()
            if self.closed:
                raise WebSocketClosed("The browser closed the DevTools connection.")
            if cancel is not None and cancel.cancelled:
                raise OperationCancelledError("Browser operation cancelled.")
            if deadline is None or time.monotonic() >= deadline:
                raise WebSocketTimeout("No answer from the browser in time.")
            time.sleep(0.002)

    def close(self) -> None:
        self.closed = True

    # --- replies
    def reply(self, message: dict[str, Any], *, result: Any = None, error: str = "") -> None:
        answer: dict[str, Any] = {"id": message["id"]}
        if message.get("sessionId"):
            answer["sessionId"] = message["sessionId"]
        if error:
            answer["error"] = {"code": -32000, "message": error}
        else:
            answer["result"] = result
        self.inbox.append(json.dumps(answer))

    def event(self, method: str, *, session: str = "", **params: Any) -> None:
        message: dict[str, Any] = {"method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.inbox.append(json.dumps(message))

    def session_of(self, tab: str) -> str:
        return next((s for s, t in self.chrome.sessions.items() if t == tab), "")

    def load(self, tab: str, url: str) -> None:
        chrome, session = self.chrome, self.session_of(tab)
        data = chrome.tabs[tab]
        self.event("Page.frameStartedLoading", session=session, frameId=f"main-{tab}")
        data["url"] = data["title"] = chrome.redirect.get(url, url)
        del data["history"][data["index"] + 1 :]
        data["history"].append(data["url"])
        data["index"] = len(data["history"]) - 1
        self.event("Page.frameNavigated", session=session, frame={"id": f"main-{tab}", "url": data["url"]})
        self.event("Page.frameStoppedLoading", session=session, frameId=f"main-{tab}")
        self.event("Target.targetInfoChanged", targetInfo=chrome.info(tab))

    def apply(self, message: dict[str, Any], tab: str) -> Any:
        chrome = self.chrome
        method, params = message["method"], message.get("params") or {}
        if method == "Target.getTargets":
            return {"targetInfos": [chrome.info(t) for t in chrome.tabs]}
        if method == "Target.createTarget":
            return {"targetId": chrome.new_tab(params.get("url") or "about:blank")}
        if method == "Target.attachToTarget":
            target = params["targetId"]
            if target not in chrome.tabs:
                return self.reply(message, error="No target with given id found")
            session = f"s{chrome.next_id}"
            chrome.next_id += 1
            chrome.sessions[session] = target
            return {"sessionId": session}
        if method == "Target.closeTarget":
            if params["targetId"] not in chrome.tabs:
                return self.reply(message, error="No target with given id found")
            chrome.close_tab(params["targetId"])
            return {"success": True}
        if method == "Browser.getVersion":
            return {"product": "FakeChrome"}
        if tab == "browser":
            return {}
        data = chrome.tabs[tab]
        session = self.session_of(tab)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": f"main-{tab}"}}}
        if method == "Page.navigate":
            url = params["url"]
            if chrome.dialog:
                self.event("Page.javascriptDialogOpening", session=session, type=chrome.dialog, message="Leave site?")
            if url in chrome.download_on:
                guid = f"g{chrome.next_id}"
                chrome.next_id += 1
                self.event("Browser.downloadWillBegin", guid=guid, url=url, suggestedFilename=chrome.download_on[url])
                self.event("Browser.downloadProgress", guid=guid, state="completed", receivedBytes=6, totalBytes=6)
                return {"frameId": f"main-{tab}", "errorText": "net::ERR_ABORTED"}
            if chrome.nav_error:
                return {"errorText": chrome.nav_error}
            self.load(tab, url)
            return {"frameId": f"main-{tab}", "loaderId": "l1"}
        if method == "Page.getNavigationHistory":
            return {
                "currentIndex": data["index"],
                "entries": [{"id": i + 1, "url": u} for i, u in enumerate(data["history"])],
            }
        if method == "Page.navigateToHistoryEntry":
            data["index"] = params["entryId"] - 1
            data["url"] = data["title"] = data["history"][data["index"]]
            self.event("Page.frameStartedLoading", session=session, frameId=f"main-{tab}")
            self.event("Page.frameStoppedLoading", session=session, frameId=f"main-{tab}")
            return {}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression"))
            if expression == "location.href":
                return {"result": {"value": data["url"]}}
            if expression == "document.readyState":
                return {"result": {"value": "complete"}}
            if expression == "window.scrollY":
                return {"result": {"value": data.get("scroll", 0)}}
            if expression.startswith("window.scrollTo"):
                data["scroll"] = float(expression.split(",")[1].strip(" )"))
                return {"result": {"value": None}}
            if "querySelectorAll('[data-highhx-id]')" in expression:  # an observation of the page
                page = {"title": data["title"], "url": data["url"], "elements": [], "text": "", "ready": "complete"}
                return {"result": {"value": page}}
            if "data-highhx-id" in expression and '"click"' in expression and chrome.on_click is not None:
                chrome.on_click(self, tab)
            if "data-highhx-id" in expression and '"locate"' in expression:
                return {"result": {"value": {"found": True, "x": 10, "y": 20}}}
            if "data-highhx-id" in expression:
                return {"result": {"value": {"found": True, "href": chrome.click_href}}}
            return {"result": {"value": 0}}
        if method == "Page.handleJavaScriptDialog":
            return None  # answered; the client ignores it by id
        return {}


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> FakeChrome:
    fake = FakeChrome()
    FakeSocket.chrome = fake
    monkeypatch.setattr(cdp_module, "WebSocket", FakeSocket)
    return fake


@pytest.fixture
def browser(chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChromeBrowser:
    b = ChromeBrowser(tmp_path / "state", headless=True, binary="/bin/chrome")
    started: list[int] = []

    def start(cancel: Any = None) -> dict[str, Any]:
        if not chrome.alive:
            chrome.restart()
            started.append(chrome.pid)
        return {"pid": chrome.pid, "port": 9}

    monkeypatch.setattr(b, "start", start)
    monkeypatch.setattr(b, "_devtools", lambda port, path, method="GET": {"webSocketDebuggerUrl": "ws://127.0.0.1/b"})
    monkeypatch.setattr(browser_module, "_alive", lambda pid: chrome.alive and pid == chrome.pid)
    monkeypatch.setattr(browser_module, "SUBFRAME_GRACE", 0.0)
    monkeypatch.setattr(browser_module, "NAVIGATION_GRACE", 0.05)
    monkeypatch.setattr(browser_module, "NEW_TAB_GRACE", 0.3)
    monkeypatch.setattr(browser_module, "DOWNLOAD_GRACE", 0.3)
    monkeypatch.setattr(browser_module, "SETUP_TIMEOUT", 0.05)
    monkeypatch.setattr(browser_module, "READY_TIMEOUT", 0.05)
    monkeypatch.setattr(ChromeBrowser, "_pause", staticmethod(lambda cancel, seconds=0.05: False))
    b.started = started  # type: ignore[attr-defined]
    return b


def _sent(chrome: FakeChrome, method: str) -> list[tuple[str, dict[str, Any]]]:
    return [(tab, params) for tab, m, params in chrome.sent if m == method]


def _events(browser: ChromeBrowser) -> list[str]:
    return [e["event"] for e in browser.drain_journal()]


def _url(chrome: FakeChrome, browser: ChromeBrowser) -> str:
    return str(chrome.tabs[browser._target_id]["url"])


# ------------------------------------------------------------- transport
def test_failed_send_is_a_closed_connection_not_a_raw_os_error() -> None:
    a, b = socket.socketpair()
    ws = WebSocket.__new__(WebSocket)
    ws.sock, ws.closed, ws._buffer = a, False, b""
    b.close()
    a.close()  # sendall on a closed socket raises OSError
    with pytest.raises(WebSocketClosed, match="lost while sending"):
        ws.send("{}")
    assert ws.closed
    ws.close()  # closing again is a no-op, not an error


def test_timeout_between_messages_keeps_the_connection_and_ignores_the_late_answer(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/b")
    session = conn.call("Target.attachToTarget", {"targetId": "t1", "flatten": True})["sessionId"]
    conn.track_session(session, "t1")
    chrome.stalled.add("t1")
    with pytest.raises(BrowserTimeoutError, match="did not answer"):
        conn.call("Runtime.evaluate", {"expression": "1"}, session_id=session, timeout=0.01)
    assert conn.usable
    first = next(conn._ids) - 1  # the id the stalled command used
    socket_ = chrome.socket
    assert socket_ is not None
    socket_.reply({"id": first, "sessionId": session}, result={"result": {"value": "late"}})
    assert conn.call("Browser.getVersion") == {"product": "FakeChrome"}  # not the late answer


def test_undelivered_command_is_disconnected_not_unknown(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/b")
    chrome.drop_before.append("Target.getTargets")
    with pytest.raises(BrowserDisconnectedError, match=r"before Target\.getTargets was sent"):
        conn.call("Target.getTargets")
    assert _sent(chrome, "Target.getTargets") == []  # nothing reached the browser


def test_a_command_to_a_session_that_is_gone_was_not_delivered(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/b")
    conn.track_session("s-old", "t1")
    with pytest.raises(BrowserDisconnectedError):
        conn.call("Runtime.evaluate", {"expression": "1"}, session_id="s-old")


def test_crash_is_detected_at_once_and_the_session_is_unusable(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/b")
    session = conn.call("Target.attachToTarget", {"targetId": "t1", "flatten": True})["sessionId"]
    conn.track_session(session, "t1")
    chrome.crash_on.append("Runtime.evaluate")
    with pytest.raises(PageCrashedError, match="crashed") as info:
        conn.call("Runtime.evaluate", {"expression": "1"}, session_id=session)
    assert isinstance(info.value, OutcomeUnknownError)  # a crashed click is never repeated
    count = len(chrome.sent)
    with pytest.raises(PageCrashedError):
        conn.call("Runtime.evaluate", {"expression": "2"}, session_id=session)
    assert len(chrome.sent) == count  # not even sent


def test_a_tab_closed_during_a_command_is_an_unknown_outcome(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/b")
    session = conn.call("Target.attachToTarget", {"targetId": "t1", "flatten": True})["sessionId"]
    conn.track_session(session, "t1")
    chrome.close_tab_on.append("Runtime.evaluate")
    with pytest.raises(TargetClosedError, match="tab closed"):
        conn.call("Runtime.evaluate", {"expression": "1"}, session_id=session)
    assert conn.usable  # the browser connection itself is fine


@pytest.mark.parametrize(
    ("kind", "accept"), [("alert", True), ("confirm", False), ("prompt", False), ("beforeunload", False)]
)
def test_dialogs_are_closed_with_the_safe_answer_and_audited(
    browser: ChromeBrowser, chrome: FakeChrome, kind: str, accept: bool
) -> None:
    assert dialog_answer(kind) is accept
    chrome.dialog = kind
    browser.navigate("https://a.test/")
    assert _sent(chrome, "Page.handleJavaScriptDialog") == [("t1", {"accept": accept})]
    dialog = next(e for e in browser.drain_journal() if e["event"] == "dialog")
    assert dialog["dialog"] == kind and dialog["accepted"] is accept
    assert ("accepted" if accept else "dismissed") in dialog["detail"]


# ------------------------------------------------------------- lifecycle
def test_browser_starts_and_one_connection_serves_sequential_actions(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    for url in ["https://github.com/", "https://wikipedia.org/", "https://example.com/"] * 7:
        result = browser.navigate(url)
        assert result.url == url and result.tab == "t1"
    browser.click("e1")
    browser.wait_ready()
    browser.type_text("e2", "hello")
    browser.press("enter")
    browser.scroll("down")
    browser.hover("e1")
    assert chrome.connections == 1 and browser.reconnects == 0
    assert len(_sent(chrome, "Target.attachToTarget")) == 1  # one session for the whole sequence
    assert _events(browser) == ["connected"]
    setup = [m for t, m, _ in chrome.sent if t == "t1"][:5]
    assert setup == [
        "Page.enable",
        "Runtime.enable",
        "Inspector.enable",
        "Page.getFrameTree",
        "Emulation.setFocusEmulationEnabled",
    ]
    download = _sent(chrome, "Browser.setDownloadBehavior")[0][1]
    assert download == {"behavior": "allowAndName", "downloadPath": str(browser.downloads_dir), "eventsEnabled": True}


def test_browser_already_running_is_reused(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.tabs["t1"]["url"] = "https://already.test/"
    assert browser.current_url() == "https://already.test/"
    assert browser.started == []  # type: ignore[attr-defined]


def test_the_working_tab_is_remembered_across_invocations(
    browser: ChromeBrowser, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    browser.state_file.parent.mkdir(parents=True, exist_ok=True)
    browser.state_file.write_text(json.dumps({"pid": chrome.pid, "port": 9}))
    browser.navigate("https://a.test/")
    used = browser._target_id
    assert json.loads(browser.state_file.read_text())["tab"] == used  # remembered in the state file
    chrome.new_tab("https://newer.test/")  # a newer tab: it is not where HighhX was working
    later = ChromeBrowser(browser.state_dir, headless=True, binary="/bin/chrome")
    monkeypatch.setattr(later, "start", lambda cancel=None: {"pid": chrome.pid, "port": 9})
    monkeypatch.setattr(later, "_devtools", browser._devtools)
    assert later.current_url() == "https://a.test/" and later._target_id == used


def test_a_stale_connection_is_checked_before_reuse(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    assert browser._conn is not None
    browser._conn.last_answer -= 60  # silent for a minute
    chrome.drop_after.append(("Browser.getVersion", False))  # … and dead
    assert browser.current_url() == "https://a.test/"
    assert browser.reconnects == 1 and "connection_lost" in _events(browser)


def test_connection_lost_between_commands_reconnects_to_the_same_tab(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.navigate("https://a.test/")
    chrome.new_tab("https://popup.test/")
    assert browser._conn is not None
    browser._conn.ws.close()
    assert browser.current_url() == "https://a.test/" and browser._target_id == "t1"
    assert browser.reconnects == 1


def test_connection_gives_up_after_bounded_attempts(
    browser: ChromeBrowser, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def refused(*_: Any, **__: Any) -> Any:
        calls["n"] += 1
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(browser, "_devtools", refused)
    with pytest.raises(IntegrationError, match="after 3 attempts") as info:
        browser.current_url()
    assert calls["n"] == 3 and "computer browser stop" in (info.value.hint or "")
    assert browser.state == BrowserState.RECOVERY_REQUIRED


def test_browser_crash_is_detected_and_the_browser_restarted(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://github.com/")
    chrome.kill()
    result = browser.navigate("https://wikipedia.org/")
    assert result.url == "https://wikipedia.org/"
    assert browser.started == [4243]  # type: ignore[attr-defined]
    events = _events(browser)
    assert "browser_gone" in events and "page_closed" in events
    assert browser.state == BrowserState.PAGE_VALID


def test_browser_restarted_externally(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://github.com/")
    chrome.kill()
    chrome.restart()  # someone else started it again (new pid, new tabs)
    assert browser.navigate("https://example.com/").url == "https://example.com/"
    assert browser.started == []  # type: ignore[attr-defined]  # not started twice


# ------------------------------------------------------------- tabs
def test_page_closed_by_the_person_then_open_continues_in_another_tab(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.navigate("https://github.com/")
    other = chrome.new_tab("https://kept.test/")
    chrome.close_tab("t1")  # the person closes the tab HighhX was using
    result = browser.navigate("https://wikipedia.org/")
    assert result.url == "https://wikipedia.org/" and result.tab == other
    assert _events(browser)[-2:] == ["page_closed", "tab_changed"]


def test_last_tab_closed_then_open_creates_a_new_tab(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://github.com/")
    chrome.close_tab("t1")
    result = browser.navigate("https://wikipedia.org/")
    assert result.url == "https://wikipedia.org/" and result.tab not in ("", "t1")
    assert "tab_created" in _events(browser)


def test_tab_closed_during_navigation_is_recovered_and_navigation_retried(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.current_url()
    chrome.close_tab_on.append("Page.navigate")
    result = browser.navigate("https://wikipedia.org/")
    assert result.url == "https://wikipedia.org/" and result.attempts == 2


def test_a_tab_that_vanishes_between_listing_and_attaching_is_skipped(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.current_url()
    browser._session_id = ""
    browser.tabs.update({"targetId": "ghost", "type": "page", "url": "https://ghost.test/"})
    browser._target_id = "ghost"
    assert browser.current_url() == "about:blank"


def test_new_tab_is_created_verified_and_used(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    result = browser.new_tab("https://b.test/")
    assert result.tab != "t1" and result.url == "https://b.test/"
    assert browser._target_id == result.tab and set(chrome.tabs) == {"t1", result.tab}
    assert [t["active"] for t in browser.list_tabs() if t["id"] == result.tab] == [True]


def test_close_tab_continues_in_the_most_recently_used(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    b = browser.new_tab("https://b.test/").tab
    browser.switch_tab("a.test")
    browser.new_tab("https://c.test/")
    continued = browser.close_tab()
    assert continued is not None and continued["id"] == "t1"  # a.test was used after b.test
    assert b in chrome.tabs


def test_switch_tab_by_url_or_title(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.new_tab("https://docs.example.test/guide")
    assert browser.switch_tab("docs.example")["url"] == "https://docs.example.test/guide"
    with pytest.raises(NotFoundError):
        browser.switch_tab("nothing-like-it")


def test_open_reuses_a_tab_already_showing_the_url(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.redirect["https://mail.test/"] = "https://mail.test/u/0/#inbox"
    browser.navigate("https://mail.test/")  # t1, redirected
    browser.new_tab("https://other.test/")
    navigations = len(_sent(chrome, "Page.navigate"))
    result = browser.navigate("https://mail.test/", reuse_tab=True)
    assert result.reused_tab and result.tab == "t1" and len(_sent(chrome, "Page.navigate")) == navigations
    assert result.redirected  # it shows where the site sent it


# ------------------------------------------------------------- navigation
def test_navigation_redirects_are_observed_not_assumed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.redirect["https://wikipedia.org/"] = "https://www.wikipedia.org/"
    chrome.redirect["http://example.com/"] = "https://example.com/"
    www = browser.navigate("https://wikipedia.org/")
    assert www.url == "https://www.wikipedia.org/" and not www.redirected  # www only: same document
    https = browser.navigate("http://example.com/")
    assert https.url == "https://example.com/" and https.redirected


def test_navigation_is_reissued_when_the_connection_drops_before_it_ran(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.current_url()
    chrome.drop_after.append(("Page.navigate", False))
    result = browser.navigate("https://a.test/")
    assert result.url == "https://a.test/" and result.attempts == 2
    assert [tab for tab, _ in _sent(chrome, "Page.navigate")] == ["t1", "t1"]
    assert "recovered" in _events(browser)


def test_navigation_that_already_happened_is_not_requested_again(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.drop_after.append(("Page.navigate", True))  # it ran; only the answer was lost
    browser.navigate("https://a.test/")
    assert len(_sent(chrome, "Page.navigate")) == 1
    assert "navigation_confirmed" in _events(browser)


def test_undelivered_navigation_is_sent_on_a_new_connection(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.drop_before.append("Page.navigate")
    browser.navigate("https://a.test/")
    assert _url(chrome, browser) == "https://a.test/" and len(_sent(chrome, "Page.navigate")) == 1


def test_navigation_gives_up_after_bounded_attempts(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    for _ in range(3):
        chrome.drop_after.append(("Page.navigate", False))
    with pytest.raises(IntegrationError, match="failed 3 times") as info:
        browser.navigate("https://a.test/")
    assert len(info.value.details) == 3 and len(_sent(chrome, "Page.navigate")) == 3
    assert browser.state == BrowserState.NAVIGATION_FAILED


def test_crash_during_navigation_replaces_the_tab_and_retries(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.crash_on.append("Page.navigate")
    result = browser.navigate("https://a.test/")
    assert "t1" not in chrome.tabs and result.url == "https://a.test/"
    assert "page_replaced" in _events(browser)


@pytest.mark.parametrize(
    ("error", "hint"),
    [
        ("net::ERR_NAME_NOT_RESOLVED", "does not exist"),
        ("net::ERR_INTERNET_DISCONNECTED", "offline"),
        ("net::ERR_CONNECTION_REFUSED", "Nothing is listening"),
        ("net::ERR_CERT_DATE_INVALID", "certificate"),
        ("net::ERR_SOMETHING_NEW", "Check the address"),
    ],
)
def test_network_failures_are_explained_and_not_retried(
    browser: ChromeBrowser, chrome: FakeChrome, error: str, hint: str
) -> None:
    chrome.nav_error = error
    with pytest.raises(IntegrationError, match=error) as info:
        browser.navigate("https://nowhere.test/")
    assert hint in (info.value.hint or "") and hint in _network_hint(error)
    assert len(_sent(chrome, "Page.navigate")) == 1
    assert browser.state == BrowserState.NAVIGATION_FAILED
    chrome.nav_error = ""
    assert browser.navigate("https://a.test/").url == "https://a.test/"  # usable right after


def test_transient_network_errors_are_retried_with_backoff(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    original = FakeSocket.apply
    failures = deque(["net::ERR_TIMED_OUT", "net::ERR_CONNECTION_RESET"])

    def flaky(self: FakeSocket, message: dict[str, Any], tab: str) -> Any:
        if message["method"] == "Page.navigate" and failures:
            return {"frameId": f"main-{tab}", "errorText": failures.popleft()}
        return original(self, message, tab)

    FakeSocket.apply = flaky  # type: ignore[method-assign]
    try:
        result = browser.navigate("https://a.test/")
    finally:
        FakeSocket.apply = original  # type: ignore[method-assign]
    assert result.url == "https://a.test/" and result.attempts == 3
    assert _events(browser).count("network_retry") == 2


def test_transient_network_errors_give_up_after_bounded_attempts(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.nav_error = "net::ERR_TIMED_OUT"
    with pytest.raises(IntegrationError, match="ERR_TIMED_OUT") as info:
        browser.navigate("https://a.test/")
    assert len(_sent(chrome, "Page.navigate")) == 3 and len(info.value.details) == 2


def test_a_stalled_tab_is_stopped_so_it_answers_again(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.stalled.add("t1")  # e.g. a Ctrl+C'd command left a load that never commits
    browser.navigate("https://a.test/")
    assert "t1" not in chrome.stalled and _url(chrome, browser) == "https://a.test/"
    assert "stalled_load" in _events(browser)


def test_a_site_that_does_not_respond_is_reported_not_retried(
    browser: ChromeBrowser, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_module, "NAVIGATE_TIMEOUT", 0.01)
    browser.current_url()
    chrome.stall_navigation = True
    with pytest.raises(IntegrationError, match="did not respond") as info:
        browser.navigate("https://slow.test/")
    assert not isinstance(info.value, OutcomeUnknownError)
    assert len(_sent(chrome, "Page.navigate")) == 1 and not chrome.stalled
    assert browser.navigate("https://a.test/").url == "https://a.test/"


def test_a_slow_page_ends_the_wait_without_an_error(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    assert browser._conn is not None
    browser._conn.sessions[browser._session_id].loading_frames.add("main-t1")  # never finishes
    browser.wait_ready(timeout=0.2)
    assert browser.last_wait_settled is False


def test_cancel_stops_the_operation_and_the_next_one_works(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    """Ctrl+C while a load hangs, then the next command in the same session: it must not wait
    for the abandoned load (the tab holds commands until that load is stopped)."""
    browser.navigate("https://a.test/")
    token = CancellationToken()
    chrome.stall_navigation = True
    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(OperationCancelledError):
        browser.navigate("https://slow.test/", cancel=token)
    assert time.monotonic() - started < 2
    assert browser.navigate("https://b.test/").url == "https://b.test/"  # recovered after Ctrl+C
    assert _sent(chrome, "Page.stopLoading")


def test_history_goes_to_exact_entries(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    for url in ("https://a.test/", "https://b.test/", "https://c.test/"):
        browser.navigate(url)
    assert browser.go_back() == "https://b.test/" and _url(chrome, browser) == "https://b.test/"
    assert browser.go_forward() == "https://c.test/"
    assert _sent(chrome, "Page.navigateToHistoryEntry")[-1][1] == {"entryId": 4}


def test_navigation_to_a_file_is_a_download_not_a_page(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.download_on["https://a.test/report.pdf"] = "report.pdf"
    guid = f"g{chrome.next_id}"
    browser.downloads_dir.mkdir(parents=True, exist_ok=True)
    (browser.downloads_dir / guid).write_bytes(b"report")  # the browser saved it under its guid
    result = browser.navigate("https://a.test/report.pdf")
    assert result.download is not None and result.download["state"] == "completed"
    assert Path(result.download["path"]) == browser.downloads_dir / "report.pdf"
    assert (browser.downloads_dir / "report.pdf").read_bytes() == b"report"
    assert "download_completed" in _events(browser)


def test_download_names_never_overwrite(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    browser.downloads_dir.mkdir(parents=True, exist_ok=True)
    (browser.downloads_dir / "report.pdf").write_bytes(b"old")
    chrome.download_on["https://a.test/report.pdf"] = "report.pdf"
    (browser.downloads_dir / f"g{chrome.next_id}").write_bytes(b"new")
    result = browser.navigate("https://a.test/report.pdf")
    assert result.download is not None and result.download["path"].endswith("report (1).pdf")
    assert (browser.downloads_dir / "report.pdf").read_bytes() == b"old"


# ------------------------------------------------------------- action safety
def test_a_lost_click_answer_is_never_resent(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    clicks: list[str] = []
    chrome.on_click = lambda ws, tab: clicks.append(tab)
    chrome.drop_after.append(("Runtime.evaluate", True))  # the click ran; the answer was lost
    with pytest.raises(OutcomeUnknownError, match="may or may not have happened") as info:
        browser.click("e1")
    assert clicks == ["t1"]
    assert any("the page is at https://a.test/" in d for d in info.value.details)  # it looked
    assert "not_repeated" in _events(browser)
    assert browser.current_url() == "https://a.test/"  # and the browser is usable


def test_a_click_that_never_reached_the_page_is_sent_once(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    clicks: list[str] = []
    chrome.on_click = lambda ws, tab: clicks.append(tab)
    chrome.drop_before.append("Runtime.evaluate")
    browser.click("e1")
    assert clicks == ["t1"]


def test_a_click_in_a_tab_that_closed_is_not_repeated(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.new_tab("https://other.test/")
    chrome.close_tab_on.append("Runtime.evaluate")
    with pytest.raises(OutcomeUnknownError):
        browser.click("e1")
    clicks = [p for _, m, p in chrome.sent if m == "Runtime.evaluate" and '"click"' in str(p.get("expression"))]
    assert len(clicks) == 1


def test_typing_is_not_repeated_after_a_lost_answer(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.drop_after.append(("Input.insertText", True))
    with pytest.raises(OutcomeUnknownError):
        browser.type_text("e2", "hello")
    assert len(_sent(chrome, "Input.insertText")) == 1


def test_key_presses_are_not_repeated_after_a_lost_answer(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.drop_after.append(("Input.dispatchKeyEvent", True))
    with pytest.raises(OutcomeUnknownError):
        browser.press("enter")
    assert len(_sent(chrome, "Input.dispatchKeyEvent")) == 1


def test_scrolling_is_exact_so_a_retry_cannot_scroll_twice(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    browser.scroll("down")
    chrome.drop_after.append(("Runtime.evaluate", False))  # the position read is lost: it is a read
    browser.scroll("down")
    evaluations = len(_sent(chrome, "Runtime.evaluate"))
    chrome.drop_after.append(("Runtime.evaluate", True))
    browser.scroll("down")  # the scroll's read ran, answer lost: read again; the scroll is exact
    assert chrome.tabs["t1"]["scroll"] == 1800  # three scrolls, never four
    assert len(_sent(chrome, "Runtime.evaluate")) == evaluations + 3


# ------------------------------------------------------------- new tabs and popups
def test_a_link_opening_its_page_in_a_new_tab_is_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://docs.test/"
    opened: list[str] = []
    chrome.on_click = lambda ws, tab: opened.append(chrome.new_tab("https://docs.test/", opener=tab))
    browser.click("e1")
    browser.wait_ready()
    assert browser._target_id == opened[0] and browser.current_url() == "https://docs.test/"
    assert "tab_followed" in _events(browser)


def test_a_sign_in_window_is_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    url = "https://accounts.google.com/o/oauth2/v2/auth?client_id=x"
    chrome.on_click = lambda ws, tab: chrome.new_tab(url, opener=tab)
    browser.click("e1")  # a "Continue with Google" button (no link)
    browser.wait_ready()
    assert browser.current_url() == url


def test_an_ad_popup_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.on_click = lambda ws, tab: chrome.new_tab("https://ads.example/win", opener=tab)
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"
    assert "popup_ignored" in _events(browser)


def test_a_second_ad_popup_is_not_followed_either(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://a.test/prize"
    chrome.on_click = lambda ws, tab: [
        chrome.new_tab("https://ads.example/1", opener=tab),
        chrome.new_tab("https://ads.example/2", opener=tab),
    ]
    browser.click("e1")
    browser.wait_ready()
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"
    assert _events(browser).count("popup_ignored") == 4


def test_a_popup_beside_a_navigation_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://ad.test/"  # even when the popup shows the link's address

    def navigate_and_popup(ws: FakeSocket, tab: str) -> None:
        chrome.new_tab("https://ad.test/", opener=tab)
        ws.load(tab, "https://a.test/next")

    chrome.on_click = navigate_and_popup
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/next"


def test_a_tab_opened_by_another_tab_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://docs.test/"
    other = chrome.new_tab("https://other.test/")
    chrome.on_click = lambda ws, tab: chrome.new_tab("https://docs.test/", opener=other)
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"


# ------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    ("requested", "actual", "same"),
    [
        ("https://example.com/", "https://www.example.com/", True),
        ("https://www.example.com/docs", "https://example.com/docs/intro", True),
        ("https://mail.google.com/", "https://accounts.google.com/signin", False),
        ("https://example.com/a", "https://example.com/login?next=/a", False),
    ],
)
def test_arrival_allows_www_redirects_and_deeper_paths(requested: str, actual: str, same: bool) -> None:
    assert _same_page(requested, actual) is same
    assert arrived(requested, actual) is same


def test_same_document_ignores_fragment_slash_and_www() -> None:
    assert same_document("https://a.test/x/", "https://www.a.test/x#top")
    assert not same_document("https://a.test/x", "")
    assert not same_document("https://a.test/x", "https://a.test/y")
    assert not same_document("http://a.test/", "https://a.test/")


@pytest.mark.parametrize(
    ("url", "sign_in"),
    [
        ("https://accounts.google.com/o/oauth2/v2/auth?x=1", True),
        ("https://github.com/login/oauth/authorize?client_id=1", True),
        ("https://login.microsoftonline.com/common/oauth2/v2.0/authorize", True),
        ("https://auth.example.com/login", True),
        ("https://github.com/python/cpython", False),
        ("https://ads.example/login", False),
        ("https://example.com/", False),
    ],
)
def test_sign_in_windows(url: str, sign_in: bool) -> None:
    assert is_sign_in_window(url) is sign_in


def test_tab_registry_prefers_tabs_highhx_used_over_popups() -> None:
    tabs = TabRegistry()
    tabs.update({"targetId": "a", "type": "page", "url": "https://a.test/"})
    tabs.update({"targetId": "ad", "type": "page", "url": "https://ad.test/", "openerId": "a"})
    tabs.update({"targetId": "w", "type": "service_worker", "url": "https://a.test/sw.js"})
    assert set(tabs.tabs) == {"a", "ad"}
    best = tabs.best(exclude={"a"})
    assert best is not None and best.id == "ad"  # the only one left
    tabs.update({"targetId": "b", "type": "page", "url": "https://b.test/"})
    best = tabs.best(exclude={"a"})
    assert best is not None and best.id == "b"  # a tab someone chose, not the popup
    tabs.sync([{"targetId": "b", "type": "page", "url": "https://b.test/"}])
    assert set(tabs.tabs) == {"b"}


def test_hung_browser_on_the_highhx_profile_is_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = ChromeBrowser(tmp_path, headless=True, binary="/bin/chrome")
    b.state_file.write_text(json.dumps({"pid": 4242, "port": 1}))
    killed: list[int] = []
    monkeypatch.setattr(browser_module, "_alive", lambda pid: True)
    monkeypatch.setattr(browser_module, "_terminate", killed.append)
    monkeypatch.setattr(browser_module, "_uses_profile", lambda pid, profile: profile == b.profile_dir)
    assert b._stop_unresponsive() and killed == [4242] and not b.state_file.exists()


def test_a_recycled_pid_is_never_killed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    b = ChromeBrowser(tmp_path, headless=True, binary="/bin/chrome")
    b.state_file.write_text(json.dumps({"pid": 4242, "port": 1}))
    killed: list[int] = []
    monkeypatch.setattr(browser_module, "_alive", lambda pid: True)
    monkeypatch.setattr(browser_module, "_terminate", killed.append)
    monkeypatch.setattr(browser_module, "_uses_profile", lambda pid, profile: False)
    assert not b._stop_unresponsive() and killed == []


def test_uses_profile_checks_the_real_command_line(tmp_path: Path) -> None:
    import os
    import sys

    if sys.platform.startswith("win"):
        pytest.skip("POSIX only")
    assert not browser_module._uses_profile(os.getpid(), tmp_path)


# ------------------------------------------------------------- runtime: gate, verification, audit
@pytest.fixture
def app(tmp_path: Path) -> Any:
    from highhx.commands import App
    from highhx.core.context import Options

    application = App(Options(interactive=True), cwd=tmp_path)
    yield application
    application.close()


def _runtime(app: Any, browser: ChromeBrowser) -> Any:
    from highhx.computer.runtime import ComputerRuntime
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode
    from tests.unit.agent.conftest import RecordingUI

    gate = ActionGate(app.engine, RecordingUI(), source="computer", mode=ApprovalMode.AUTO_EDIT, audit=AuditLog(app.db))
    return ComputerRuntime(browser, gate, actor=Actor.USER, settle=0)


def _audit(app: Any) -> list[dict[str, Any]]:
    rows = app.db.query("SELECT action, status, details FROM audit_log ORDER BY created_at, rowid")
    return [{"action": r["action"], "status": r["status"], "details": json.loads(r["details"])} for r in rows]


def test_recoveries_and_dialogs_are_written_to_the_audit_trail(
    app: Any, browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    rt = _runtime(app, browser)
    assert rt.navigate("https://github.com/").verified
    chrome.close_tab("t1")  # the person closes the tab
    chrome.dialog = "confirm"
    outcome = rt.navigate("https://wikipedia.org/")
    assert outcome.verified and outcome.observation is not None
    record = _audit(app)[-1]
    assert record["action"] == "Open https://wikipedia.org/" and record["status"] == "ok"
    events = [e["event"] for e in record["details"]["browser"]]
    assert "page_closed" in events and "dialog" in events
    assert record["details"]["navigation"]["url"] == "https://wikipedia.org/"


def test_page_actions_go_through_the_gate_and_are_verified(
    app: Any, browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    rt = _runtime(app, browser)
    rt.navigate("https://a.test/")
    rt.navigate("https://b.test/")
    back = rt.page_action("back")
    assert back.verified and back.observation is not None and back.observation.url == "https://a.test/"
    tab = rt.page_action("new_tab", "c.test")
    assert tab.verified and same_document("https://c.test/", browser.current_url())
    closed = rt.page_action("close_tab")
    assert closed.ok and browser.current_url() == "https://a.test/"
    assert [r["action"] for r in _audit(app)][-3:] == ["Go back", "Open a new tab at https://c.test", "Close the tab"]


def test_a_lost_click_is_audited_as_unknown_and_refused_again(
    app: Any, browser: ChromeBrowser, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.computer.model import Observation, UIElement

    rt = _runtime(app, browser)
    rt.navigate("https://a.test/")
    page = Observation(
        provider="browser",
        application="chrome",
        title="A",
        url="https://a.test/",
        elements=[UIElement(id="e1", role="link", name="Next", attributes={"tag": "a", "href": "/next"})],
        text="",
        captured_at=0.0,
    )
    monkeypatch.setattr(browser, "observe", lambda cancel=None: page)
    rt.observe()
    chrome.drop_after.append(("Runtime.evaluate", True))
    with pytest.raises(OutcomeUnknownError):
        rt.act("click:e1")
    record = _audit(app)[-1]
    assert record["status"] == "unknown" and "not_repeated" in [e["event"] for e in record["details"]["browser"]]
    rt.observe()
    with pytest.raises(IntegrationError, match="outcome is unknown"):
        rt.act("click:e1")

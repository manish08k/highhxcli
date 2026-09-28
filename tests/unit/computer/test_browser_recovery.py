"""Browser lifecycle recovery: reconnects, crashes, dialogs, navigation retries, new tabs.

A scripted in-memory Chrome stands in for DevTools, so every failure (a connection that
drops before or after a command, a renderer crash, a blocking dialog, a popup) is exact
and repeatable. The real browser is exercised by ``test_live_browser.py``.
"""

from __future__ import annotations

import json
import socket
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from highhx.computer import browser as browser_module
from highhx.computer.browser import (
    BrowserDisconnectedError,
    CDPConnection,
    ChromeBrowser,
    PageCrashedError,
    _network_hint,
    _same_document,
)
from highhx.computer.runtime import _same_page
from highhx.computer.websocket import WebSocket, WebSocketClosed, WebSocketTimeout
from highhx.core.errors import IntegrationError, OutcomeUnknownError


class FakeChrome:
    """Browser-wide state shared by every connection (tabs survive a dropped connection)."""

    def __init__(self) -> None:
        self.tabs: dict[str, dict[str, Any]] = {"t1": {"url": "about:blank", "crashed": False}}
        self.sent: list[tuple[str, str, dict[str, Any]]] = []
        """(tab, method, params) for every command that reached the browser."""
        self.connections = 0
        self.drop_before: deque[str] = deque()
        """Methods whose next send fails (the command never reaches the browser)."""
        self.drop_after: deque[tuple[str, bool]] = deque()
        """(method, applied): the connection dies after the command arrived (applied or not)."""
        self.crash_on: deque[str] = deque()
        self.nav_error = ""
        self.redirect: dict[str, str] = {}
        self.dialog: str = ""
        self.on_click: Any = None
        self.stalled = False
        """A navigation that never commits: the tab answers nothing but Page.stopLoading."""
        self.stall_navigation = False
        """The next Page.navigate goes to a server that never answers (and stalls the tab)."""
        self.click_href = ""
        """The resolved href of the link a click lands on ('' for a button)."""
        self.closed_tabs: list[str] = []
        self.next_tab = 2

    def new_tab(self, url: str = "about:blank") -> str:
        tab = f"t{self.next_tab}"
        self.next_tab += 1
        self.tabs[tab] = {"url": url, "crashed": False}
        return tab

    def pages(self) -> list[dict[str, Any]]:
        # like /json/list: most recently opened first
        return [
            {"id": t, "type": "page", "url": v["url"], "webSocketDebuggerUrl": f"ws://127.0.0.1/devtools/page/{t}"}
            for t, v in reversed(self.tabs.items())
        ]


class FakeSocket:
    """Stands in for :class:`WebSocket`; answers like one Chrome page target."""

    chrome: FakeChrome

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        self.tab = url.rsplit("/", 1)[-1]
        self.closed = False
        self.inbox: deque[str] = deque()
        self.waiting = False
        self.chrome.connections += 1
        self.sock = self  # tests close "the socket" like the live tests do

    # --- WebSocket API
    def send(self, text: str) -> None:
        if self.closed:
            raise WebSocketClosed("closed")
        message = json.loads(text)
        method = message["method"]
        chrome = self.chrome
        if chrome.drop_before and chrome.drop_before[0] == method:
            chrome.drop_before.popleft()
            self.closed = True
            raise WebSocketClosed("The DevTools connection was lost while sending.")
        chrome.sent.append((self.tab, method, message.get("params") or {}))
        if chrome.drop_after and chrome.drop_after[0][0] == method:
            _, applied = chrome.drop_after.popleft()
            if applied:
                self._apply(message)
            self.closed = True
            return
        if method == "Page.stopLoading":
            chrome.stalled = False
        elif chrome.stalled or (method == "Page.navigate" and chrome.stall_navigation):
            chrome.stall_navigation, chrome.stalled = False, True
            self.waiting = True  # no answer will come
            return
        if chrome.crash_on and chrome.crash_on[0] == method:
            chrome.crash_on.popleft()
            chrome.tabs[self.tab]["crashed"] = True
            self._event("Inspector.targetCrashed")
            return
        result = self._apply(message)
        if result is not None:
            self.inbox.append(json.dumps({"id": message["id"], "result": result}))

    def recv(self, *, cancel: Any = None, poll: float = 0.2, deadline: float | None = None) -> str:
        if self.inbox:
            return self.inbox.popleft()
        self.closed = True
        if self.waiting:
            raise WebSocketTimeout("No answer from the browser in time.")
        raise WebSocketClosed("The browser closed the DevTools connection.")

    def close(self) -> None:
        self.closed = True

    # --- the page
    def _event(self, method: str, **params: Any) -> None:
        self.inbox.append(json.dumps({"method": method, "params": params}))

    def _apply(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method, params = message["method"], message.get("params") or {}
        tab = self.chrome.tabs[self.tab]
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "main"}}}
        if method == "Page.navigate":
            if self.chrome.dialog:
                self._event("Page.javascriptDialogOpening", type=self.chrome.dialog, message="Leave site?")
            if self.chrome.nav_error:
                return {"errorText": self.chrome.nav_error}
            self._event("Page.frameStartedLoading", frameId="main")
            tab["url"] = self.chrome.redirect.get(params["url"], params["url"])
            self._event("Page.frameNavigated", frame={"id": "main", "url": tab["url"]})
            self._event("Page.frameStoppedLoading", frameId="main")
            return {"frameId": "main", "loaderId": "l1"}
        if method == "Runtime.evaluate":
            expression = str(params.get("expression"))
            if expression == "location.href":
                return {"result": {"value": tab["url"]}}
            if expression == "document.readyState":
                return {"result": {"value": "complete"}}
            if "click" in expression and self.chrome.on_click is not None:
                self.chrome.on_click(self)
            if "data-highhx-id" in expression:
                return {"result": {"value": {"found": True, "href": self.chrome.click_href}}}
            return {"result": {"value": 0}}
        if method == "Page.handleJavaScriptDialog":
            return None  # Chrome answers; the client ignores it — keep the inbox deterministic
        return {}


@pytest.fixture
def chrome(monkeypatch: pytest.MonkeyPatch) -> FakeChrome:
    fake = FakeChrome()
    FakeSocket.chrome = fake
    monkeypatch.setattr(browser_module, "WebSocket", FakeSocket)
    return fake


@pytest.fixture
def browser(chrome: FakeChrome, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ChromeBrowser:
    b = ChromeBrowser(tmp_path / "state", headless=True, binary="/bin/chrome")
    monkeypatch.setattr(b, "start", lambda cancel=None: {"pid": 1, "port": 9})
    monkeypatch.setattr(b, "_pages", lambda port: chrome.pages())

    def devtools(port: int, path: str, *, method: str = "GET") -> Any:
        if path.startswith("/json/new"):
            tab = chrome.new_tab()
            return next(p for p in chrome.pages() if p["id"] == tab)
        if path.startswith("/json/close/"):
            tab = path.rsplit("/", 1)[-1]
            chrome.tabs.pop(tab, None)
            chrome.closed_tabs.append(tab)
            return None
        raise AssertionError(path)

    monkeypatch.setattr(b, "_devtools", devtools)
    monkeypatch.setattr(browser_module, "SUBFRAME_GRACE", 0.0)
    monkeypatch.setattr(browser_module, "NAVIGATION_GRACE", 0.05)
    return b


def _sent(chrome: FakeChrome, method: str) -> list[tuple[str, dict[str, Any]]]:
    return [(tab, params) for tab, m, params in chrome.sent if m == method]


# --------------------------------------------------------------- transport
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


def test_undelivered_command_is_reported_as_disconnected_not_unknown(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/devtools/page/t1", target_id="t1")
    chrome.drop_before.append("Runtime.evaluate")
    with pytest.raises(BrowserDisconnectedError, match=r"before Runtime\.evaluate was sent"):
        conn.call("Runtime.evaluate", {"expression": "1"})
    assert _sent(chrome, "Runtime.evaluate") == []  # nothing reached the browser


def test_crash_is_detected_at_once_and_the_session_is_unusable(chrome: FakeChrome) -> None:
    conn = CDPConnection("ws://127.0.0.1/devtools/page/t1", target_id="t1")
    chrome.crash_on.append("Runtime.evaluate")
    with pytest.raises(PageCrashedError, match="crashed") as info:
        conn.call("Runtime.evaluate", {"expression": "1"})
    assert isinstance(info.value, OutcomeUnknownError)  # a crashed click is never repeated
    assert not conn.usable
    count = len(chrome.sent)
    with pytest.raises(PageCrashedError):
        conn.call("Runtime.evaluate", {"expression": "2"})
    assert len(chrome.sent) == count  # not even sent


@pytest.mark.parametrize(
    ("kind", "accept"), [("alert", True), ("confirm", False), ("prompt", False), ("beforeunload", False)]
)
def test_blocking_dialogs_are_closed_with_the_safe_answer(chrome: FakeChrome, kind: str, accept: bool) -> None:
    conn = CDPConnection("ws://127.0.0.1/devtools/page/t1", target_id="t1")
    chrome.dialog = kind
    conn.call("Page.navigate", {"url": "https://a.test/"})
    assert _sent(chrome, "Page.handleJavaScriptDialog") == [("t1", {"accept": accept})]
    assert conn.dialogs == [{"type": kind, "message": "Leave site?", "accepted": accept}]


# --------------------------------------------------------------- navigation
def test_normal_navigation_and_session_setup(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    assert chrome.tabs["t1"]["url"] == "https://a.test/"
    methods = [m for _, m, _ in chrome.sent]
    assert methods[:5] == [
        "Page.enable",
        "Page.getFrameTree",
        "Runtime.enable",
        "Inspector.enable",
        "Emulation.setFocusEmulationEnabled",
    ]
    download = _sent(chrome, "Page.setDownloadBehavior")[0][1]
    assert download == {"behavior": "allow", "downloadPath": str(browser.downloads_dir)}
    assert browser.downloads_dir.is_dir() and browser.last_wait_settled


def test_navigation_is_reissued_when_the_connection_drops_before_it_ran(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    browser.current_url()  # the session exists before the failure
    chrome.drop_after.append(("Page.navigate", False))  # sent, lost, not applied
    browser.navigate("https://a.test/")
    assert chrome.tabs["t1"]["url"] == "https://a.test/"
    assert len(_sent(chrome, "Page.navigate")) == 2 and browser.reconnects == 1
    assert [tab for tab, _ in _sent(chrome, "Page.navigate")] == ["t1", "t1"]  # the same tab


def test_navigation_that_already_happened_is_not_requested_again(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.drop_after.append(("Page.navigate", True))  # it ran; only the answer was lost
    browser.navigate("https://a.test/")
    assert len(_sent(chrome, "Page.navigate")) == 1  # observed the page, found it there


def test_undelivered_navigation_is_resent_on_a_new_connection(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()  # open the session
    chrome.drop_before.append("Page.navigate")
    browser.navigate("https://a.test/")
    assert chrome.tabs["t1"]["url"] == "https://a.test/" and len(_sent(chrome, "Page.navigate")) == 1


def test_navigation_gives_up_after_bounded_attempts_with_every_reason(
    browser: ChromeBrowser, chrome: FakeChrome
) -> None:
    for _ in range(3):
        chrome.drop_after.append(("Page.navigate", False))
    with pytest.raises(IntegrationError, match="failed 3 times") as info:
        browser.navigate("https://a.test/")
    assert len(info.value.details) == 3 and "attempt 1" in info.value.details[0]
    assert "computer browser stop" in (info.value.hint or "")
    assert len(_sent(chrome, "Page.navigate")) == 3


def test_crash_during_navigation_reopens_a_fresh_tab_and_retries(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.crash_on.append("Page.navigate")
    browser.navigate("https://a.test/")
    assert chrome.closed_tabs == ["t1"]
    (tab,) = chrome.tabs
    assert tab != "t1" and chrome.tabs[tab]["url"] == "https://a.test/"


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


def test_aborted_navigation_is_not_an_error(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    chrome.nav_error = "net::ERR_ABORTED"  # replaced by a download or a redirect
    browser.navigate("https://a.test/file.zip")


# --------------------------------------------------------------- connection
def test_reconnect_returns_to_the_same_tab_not_the_first_listed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.new_tab("https://popup.test/")  # listed first from now on
    assert browser._conn is not None
    browser._conn.ws.close()  # the link dies
    assert browser.current_url() == "https://a.test/"
    assert browser.reconnects == 1


def test_connection_gives_up_after_bounded_attempts(
    browser: ChromeBrowser, chrome: FakeChrome, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def refused(port: int) -> list[dict[str, Any]]:
        calls["n"] += 1
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(browser, "_pages", refused)
    monkeypatch.setattr(browser, "_pause", lambda cancel, seconds=0.05: False)
    with pytest.raises(IntegrationError, match="after 3 attempts") as info:
        browser.current_url()
    assert calls["n"] == 3 and "computer browser stop" in (info.value.hint or "")


def test_a_lost_click_answer_is_never_resent(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    clicks: list[str] = []
    chrome.on_click = lambda ws: clicks.append(ws.tab)
    chrome.drop_after.append(("Runtime.evaluate", True))  # the click ran; the answer was lost
    with pytest.raises(OutcomeUnknownError):
        browser.click("e1")
    browser.current_url()  # reconnects fine afterwards
    assert clicks == ["t1"]


def test_an_undelivered_click_is_sent_once_on_a_new_connection(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    clicks: list[str] = []
    chrome.on_click = lambda ws: clicks.append(ws.tab)
    chrome.drop_before.append("Runtime.evaluate")
    browser.click("e1")
    assert clicks == ["t1"]


def test_sequential_actions_share_one_session(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    browser.click("e1")
    browser.wait_ready()
    browser.type_text("e2", "hello")
    browser.press("enter")
    browser.wait_ready()
    browser.scroll("down")
    browser.navigate("https://b.test/")
    assert chrome.connections == 1 and browser.reconnects == 0
    assert _sent(chrome, "Input.insertText") == [("t1", {"text": "hello"})]
    assert len(_sent(chrome, "Input.dispatchKeyEvent")) == 2


# --------------------------------------------------------------- new tabs
def test_a_link_that_opens_its_page_in_a_new_tab_continues_there(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://docs.test/"
    opened: list[str] = []
    chrome.on_click = lambda ws: opened.append(chrome.new_tab("https://docs.test/"))
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://docs.test/"
    assert browser._target_id == opened[0]


def test_a_popup_storm_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://a.test/prize"
    chrome.on_click = lambda ws: [chrome.new_tab("https://ad1.test/"), chrome.new_tab("https://ad2.test/")]
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"


def test_a_button_popup_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    """One gesture allows one window.open — an ad from a button looks like any new tab."""
    browser.navigate("https://a.test/")
    chrome.on_click = lambda ws: chrome.new_tab("https://ad.test/")
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"


def test_a_link_whose_new_tab_shows_something_else_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    chrome.click_href = "https://docs.test/"
    chrome.on_click = lambda ws: chrome.new_tab("https://ad.test/")
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"


def test_a_popup_beside_a_navigation_is_not_followed(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    """The link navigated its own page and an ad opened a tab next to it: stay on the page."""
    browser.navigate("https://a.test/")

    def navigate_and_popup(ws: FakeSocket) -> None:
        chrome.new_tab("https://ad.test/")
        ws._event("Page.frameStartedLoading", frameId="main")
        chrome.tabs[ws.tab]["url"] = "https://a.test/next"
        ws._event("Page.frameStoppedLoading", frameId="main")

    chrome.click_href = "https://ad.test/"  # even when the popup shows the link's address
    chrome.on_click = navigate_and_popup
    browser.click("e1")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/next"


def test_typing_does_not_switch_tabs(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    browser.current_url()
    chrome.new_tab("https://other.test/")  # opened by something else, not by this action
    browser.type_text("e2", "x")
    browser.wait_ready()
    assert browser.current_url() == "https://a.test/"


# --------------------------------------------------------------- slow pages
def test_a_slow_page_ends_the_wait_without_an_error(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.navigate("https://a.test/")
    assert browser._conn is not None
    browser._conn.loading_frames.add("main")  # never finishes
    browser.wait_ready(timeout=0.2)
    assert browser.last_wait_settled is False


# --------------------------------------------------------------- hung browser
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


# --------------------------------------------------------------- URL checks
@pytest.mark.parametrize(
    ("requested", "actual", "same"),
    [
        ("https://example.com/", "https://www.example.com/", True),
        ("https://www.example.com/docs", "https://example.com/docs/intro", True),
        ("https://mail.google.com/", "https://accounts.google.com/signin", False),
        ("https://example.com/a", "https://example.com/login?next=/a", False),
    ],
)
def test_same_page_allows_www_redirects_only(requested: str, actual: str, same: bool) -> None:
    assert _same_page(requested, actual) is same


def test_same_document_ignores_fragment_and_trailing_slash() -> None:
    assert _same_document("https://a.test/x/", "https://a.test/x#top")
    assert not _same_document("https://a.test/x", "")
    assert not _same_document("https://a.test/x", "https://a.test/y")


# --------------------------------------------------------------- stalled loads
def test_a_stalled_load_is_stopped_so_the_tab_answers_again(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    """A client gave up on a load that never commits; the tab ignores new sessions until stopped."""
    chrome.stalled = True
    browser.navigate("https://a.test/")
    methods = [m for _, m, _ in chrome.sent]
    assert methods[:2] == ["Page.enable", "Page.stopLoading"]  # attach timed out → stop → attach again
    assert chrome.tabs["t1"]["url"] == "https://a.test/"


def test_a_site_that_does_not_respond_is_reported_not_retried(browser: ChromeBrowser, chrome: FakeChrome) -> None:
    browser.current_url()
    chrome.stall_navigation = True
    with pytest.raises(IntegrationError, match="did not respond") as info:
        browser.navigate("https://slow.test/")
    assert not isinstance(info.value, OutcomeUnknownError)
    assert len(_sent(chrome, "Page.navigate")) == 1  # never re-requested
    assert _sent(chrome, "Page.stopLoading") and not chrome.stalled  # the tab is usable again
    browser.navigate("https://a.test/")
    assert chrome.tabs["t1"]["url"] == "https://a.test/"

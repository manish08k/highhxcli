"""Real Chrome/Chromium through DevTools. Opt-in: HIGHHX_TEST_BROWSER=1 (needs a browser that
can start in this environment)."""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from highhx.commands import App
from highhx.computer.browser import ChromeBrowser, arrived, find_browser
from highhx.computer.runtime import ComputerRuntime
from highhx.core.context import Options
from highhx.core.errors import (
    ApprovalDeniedError,
    IntegrationError,
    NotFoundError,
    OperationCancelledError,
    OutcomeUnknownError,
)
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)


@pytest.fixture
def site(tmp_path: Path) -> Iterator[str]:
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Shop</title><form action='results.html'><label for=q>Search</label>"
        "<input id=q name=q type=search><button>Go</button></form>"
    )
    (tmp_path / "delete.html").write_text(
        "<!doctype html><title>Items</title><p id=n>deletions: 0</p>"
        "<button class='btn-danger' onclick=\"n.textContent='deletions: '+(++window.c||(window.c=1))\">Delete item</button>"
    )
    (tmp_path / "form.html").write_text(
        "<!doctype html><title>Settings</title><label for=e>Email</label><input id=e type=email>"
        "<label><input id=c type=checkbox>Newsletter</label><p id=n>deletions: 0</p>"
        "<button id=d class='btn btn-danger' onclick=\"n.textContent='deletions: '+(++window.c||(window.c=1))\">"
        "Delete item</button>"
    )
    (tmp_path / "results.html").write_text(
        "<!doctype html><title>Results</title><p id=r></p><script>"
        "r.innerText='You searched for '+new URLSearchParams(location.search).get('q')</script>"
    )
    (tmp_path / "links.html").write_text(
        "<!doctype html><title>Links</title><a href='results.html?q=tab' target=_blank>Docs in a new tab</a>"
        "<button onclick=\"window.open('index.html');window.open('delete.html')\">Win a prize</button>"
        "<a href='results.html?q=same' onclick=\"window.open('delete.html')\">Next page</a>"
        "<button onclick=\"alert('saved')\">Save</button>"
        "<a href='file.bin' download>Download report</a>"
    )
    (tmp_path / "file.bin").write_bytes(b"report")
    (tmp_path / "dialogs.html").write_text(
        "<!doctype html><title>Dialogs</title><p id=r>none</p>"
        "<button onclick=\"r.textContent=confirm('Delete everything?')?'confirmed':'kept'\">Delete all</button>"
        "<button onclick=\"r.textContent=prompt('Name?')===null?'no name':'named'\">Rename</button>"
    )
    (tmp_path / "interact.html").write_text(
        "<!doctype html><title>Interact</title><p id=log>-</p>"
        "<button onmouseover=\"log.textContent='hovered'\">Menu</button>"
        "<button ondblclick=\"log.textContent='double'\">Zoom</button>"
        "<div id=src draggable=true role=button aria-label=Card "
        "ondragstart=\"event.dataTransfer.setData('text','card')\">Card</div>"
        '<div id=dst role=button aria-label=Bin ondragover="event.preventDefault()" '
        "ondrop=\"event.preventDefault();log.textContent='dropped '+event.dataTransfer.getData('text')\">Bin</div>"
        "<label for=f>Attachment</label><input id=f type=file>"
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_search_in_a_real_browser(site: str, tmp_path: Path) -> None:
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/index.html")
        observation = browser.observe()
        box = next(e for e in observation.elements if e.role == "searchbox")
        browser.type_text(box.id, "Adele")
        browser.press("enter")
        browser.wait_ready()
        after = browser.observe()
        assert after.title == "Results" and "You searched for Adele" in after.text
    finally:
        assert browser.stop()


def test_reconnect_does_not_repeat_a_completed_destructive_click(site: str, tmp_path: Path) -> None:
    """Click once, drop the DevTools connection (as a crash of the link would), reconnect:
    the page shows exactly one deletion and nothing was re-sent."""
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/delete.html")
        button = next(e for e in browser.observe().elements if e.name == "Delete item")
        browser.click(button.id)
        assert browser._conn is not None
        browser._conn.ws.sock.close()  # the link dies under the client
        browser._conn.ws.closed = True
        after = browser.observe()  # lazily reconnects to the same page
        assert "deletions: 1" in after.text
    finally:
        assert browser.stop()


def _action_id(runtime: ComputerRuntime, verb: str, name: str) -> str:
    observation = runtime.observe()
    element = next(e for e in observation.elements if e.name == name)
    return f"{verb}:{element.id}"


def test_runtime_gate_and_verification_in_a_real_browser(site: str, tmp_path: Path) -> None:
    """The shared runtime against real Chrome: typed values read back from the real DOM, a
    checkbox toggle verified, a destructive click declined (not executed) and then approved
    (executed exactly once), and an approval invalidated when the control disappears."""
    app = App(Options(interactive=True), cwd=tmp_path)
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    ui = RecordingUI(action_answers=[False, True, True])
    gate = ActionGate(
        app.engine, ui, source="computer", mode=ApprovalMode.AUTO_EDIT, audit=AuditLog(app.db, app.redactor)
    )
    runtime = ComputerRuntime(browser, gate, actor=Actor.AGENT, settle=0.1)
    try:
        assert runtime.navigate(f"{site}/form.html").verified
        typed = runtime.act(_action_id(runtime, "type", "Email"), "dev@example.com")
        assert typed.verified is True
        toggled = runtime.act(_action_id(runtime, "click", "Newsletter"))
        assert toggled.verified is True

        with pytest.raises(ApprovalDeniedError):
            runtime.act(_action_id(runtime, "click", "Delete item"))
        assert "deletions: 0" in runtime.observe().text

        deleted = runtime.act(_action_id(runtime, "click", "Delete item"))
        assert deleted.verified is True and "deletions: 1" in runtime.observe().text

        action = _action_id(runtime, "click", "Delete item")
        original = ui.confirm_action

        def approve_then_remove(request: object) -> bool:
            browser._eval("document.getElementById('d').remove()", None)
            return original(request)  # type: ignore[arg-type]

        ui.confirm_action = approve_then_remove  # type: ignore[method-assign]
        with pytest.raises(NotFoundError, match="no longer on the screen"):
            runtime.act(action)
        assert "deletions: 1" in runtime.observe().text
    finally:
        assert browser.stop()
        app.close()


def _click(browser: ChromeBrowser, name: str) -> None:
    element = next(e for e in browser.observe().elements if e.name == name)
    browser.click(element.id)
    browser.wait_ready(timeout=10)


def _element(browser: ChromeBrowser, name: str) -> str:
    return next(e.id for e in browser.observe().elements if e.name == name)


def _events(browser: ChromeBrowser) -> list[dict[str, object]]:
    return browser.drain_journal()


def test_new_tabs_popups_dialogs_and_downloads_in_a_real_browser(site: str, tmp_path: Path) -> None:
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/links.html")
        first_tab = browser._target_id
        _click(browser, "Docs in a new tab")  # a legitimate new tab: continue there
        assert "You searched for tab" in browser.observe().text
        assert browser._target_id != first_tab

        browser.navigate(f"{site}/links.html")
        tab = browser._target_id
        _click(browser, "Win a prize")  # a script's popup (an ad): stay
        assert browser._target_id == tab and browser.observe().title == "Links"
        _click(browser, "Win a prize")  # … and the second one
        assert browser._target_id == tab
        assert sum(1 for e in _events(browser) if e["event"] == "popup_ignored") >= 2

        _click(browser, "Next page")  # the page navigated and a popup opened beside it: stay
        assert browser._target_id == tab and "You searched for same" in browser.observe().text

        browser.navigate(f"{site}/links.html")
        _click(browser, "Save")  # a blocking alert() is closed; the page keeps answering
        assert browser.observe().title == "Links"
        dialogs = [e for e in _events(browser) if e["event"] == "dialog"]
        assert dialogs and dialogs[-1]["dialog"] == "alert" and dialogs[-1]["accepted"] is True

        download = browser.download(_element(browser, "Download report"))
        assert download.state == "completed" and Path(download.path).read_bytes() == b"report"
        assert Path(download.path).parent == browser.downloads_dir and Path(download.path).name == "file.bin"
    finally:
        assert browser.stop()


def test_confirm_and_prompt_are_cancelled_in_a_real_browser(site: str, tmp_path: Path) -> None:
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/dialogs.html")
        _click(browser, "Delete all")
        assert "kept" in browser.observe().text  # confirm() was answered "cancel"
        _click(browser, "Rename")
        assert "no name" in browser.observe().text  # prompt() was cancelled
        decisions = [(e["dialog"], e["accepted"]) for e in _events(browser) if e["event"] == "dialog"]
        assert decisions == [("confirm", False), ("prompt", False)]
    finally:
        assert browser.stop()


def test_crash_and_connection_loss_recovery_in_a_real_browser(site: str, tmp_path: Path) -> None:
    from highhx.computer.browser import Retry

    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/index.html")
        crashed_tab = browser._target_id
        with pytest.raises(OutcomeUnknownError):
            browser._run("crash the page", Retry.UNSAFE, lambda c, s: c.call("Page.crash", session_id=s), None)
        browser.navigate(f"{site}/delete.html")  # a fresh tab, and the page opens
        assert browser._target_id != crashed_tab and browser.observe().title == "Items"

        # the connection dies right after Page.navigate is sent: reconnect, look, finish
        assert browser._conn is not None
        conn, session = browser._conn, browser._session_id
        original = conn.call

        def drop_after_navigate(method: str, params: object = None, **kwargs: object) -> dict[str, object]:
            if method == "Page.navigate":
                conn.send(method, params, session_id=session)  # type: ignore[arg-type]
                conn.ws.sock.close()
                conn.ws.closed = True
                raise OutcomeUnknownError("lost after Page.navigate was sent")
            return original(method, params, **kwargs)  # type: ignore[arg-type]

        conn.call = drop_after_navigate  # type: ignore[method-assign]
        result = browser.navigate(f"{site}/form.html")
        assert browser.observe().title == "Settings" and browser.reconnects >= 1 and result.attempts == 2

        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with pytest.raises(IntegrationError, match="ERR_CONNECTION_REFUSED") as info:
            browser.navigate(f"http://127.0.0.1:{port}/")
        assert "Nothing is listening" in (info.value.hint or "")
    finally:
        assert browser.stop()


def test_closed_tab_and_killed_browser_are_recovered_in_a_real_browser(site: str, tmp_path: Path) -> None:
    import os
    import signal
    import urllib.request

    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/index.html")
        port = browser._saved()["port"]
        urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/close/{browser._target_id}"
        ).read()  # the person closes it
        result = browser.navigate(f"{site}/delete.html")
        assert browser.observe().title == "Items" and result.attempts <= 2  # the close may race the command
        assert "page_closed" in [e["event"] for e in _events(browser)]

        os.kill(int(browser._saved()["pid"]), signal.SIGKILL)  # the browser dies
        time.sleep(0.5)
        browser.navigate(f"{site}/form.html")
        assert browser.observe().title == "Settings"
        assert "browser_gone" in [e["event"] for e in _events(browser)]
    finally:
        assert browser.stop()


def test_twenty_sequential_navigations_share_one_connection(site: str, tmp_path: Path) -> None:
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    pages = ["index.html", "delete.html", "form.html", "results.html?q=x"]
    try:
        for i in range(20):
            result = browser.navigate(f"{site}/{pages[i % 4]}")
            assert result.url.startswith(f"{site}/{pages[i % 4].split('?')[0]}")
        assert browser.reconnects == 0 and browser._conn is not None
        assert len([s for s in browser._conn.sessions if s]) == 1  # one tab session throughout
    finally:
        assert browser.stop()


def test_history_hover_double_click_drag_and_upload_in_a_real_browser(site: str, tmp_path: Path) -> None:
    upload = tmp_path / "notes.txt"
    upload.write_text("hello")
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/index.html")
        browser.navigate(f"{site}/interact.html")
        assert browser.go_back().endswith("/index.html") and browser.observe().title == "Shop"
        assert browser.go_forward().endswith("/interact.html")

        browser.hover(_element(browser, "Menu"))
        assert "hovered" in browser.observe().text
        browser.double_click(_element(browser, "Zoom"))
        assert "double" in browser.observe().text
        browser.drag(_element(browser, "Card"), _element(browser, "Bin"))
        assert "dropped card" in browser.observe().text
        field = _element(browser, "Attachment")
        browser.upload(field, [str(upload)])
        chosen = next(e for e in browser.observe().elements if e.id == field)
        assert chosen.value.endswith("notes.txt")

        browser.reload()
        assert browser.observe().title == "Interact"
    finally:
        assert browser.stop()


@pytest.fixture
def silent_server() -> Iterator[str]:
    """Accepts connections and never answers (a hung site)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        server.settimeout(0.2)
        while not stop.is_set():
            with contextlib.suppress(OSError):
                held.append(server.accept()[0])

    threading.Thread(target=accept, daemon=True).start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/"
    stop.set()
    for sock in [*held, server]:
        sock.close()


def test_a_stalled_load_never_leaves_the_browser_unusable(
    site: str, silent_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from highhx.computer import browser as browser_module
    from highhx.execution.cancellation import CancellationToken

    monkeypatch.setattr(browser_module, "NAVIGATE_TIMEOUT", 3.0)
    state = tmp_path / "state"
    browser = ChromeBrowser(state, headless=True)
    try:
        with pytest.raises(IntegrationError, match="did not respond"):
            browser.navigate(silent_server)
        browser.navigate(f"{site}/index.html")  # at once, not after a hang
        assert browser.observe().title == "Shop"

        # Ctrl+C during a hanging load, then the next command in the same session
        token = CancellationToken()
        threading.Timer(0.5, token.cancel).start()
        with pytest.raises(OperationCancelledError):
            browser.navigate(silent_server, cancel=token)
        started = time.monotonic()
        browser.navigate(f"{site}/form.html")
        assert browser.observe().title == "Settings" and time.monotonic() - started < 10

        # a client that gives up mid-load and exits — the next process must still get the tab
        assert browser._conn is not None
        browser._conn.send("Page.navigate", {"url": silent_server}, session_id=browser._session_id)
        time.sleep(0.5)
        browser.close()
        started = time.monotonic()
        other = ChromeBrowser(state, headless=True)
        other.navigate(f"{site}/delete.html")
        assert other.observe().title == "Items" and time.monotonic() - started < 15
        other.close()
    finally:
        assert browser.stop()


def _online() -> bool:
    try:
        socket.create_connection(("github.com", 443), timeout=5).close()
    except OSError:
        return False
    return True


@pytest.mark.skipif(not os.environ.get("HIGHHX_TEST_BROWSER"), reason="set HIGHHX_TEST_BROWSER=1")
def test_github_then_wikipedia_repeatedly_on_the_real_internet(tmp_path: Path) -> None:
    """The reported regression: open GitHub, then Wikipedia (and Example), over and over,
    in one session — every navigation arrives, on one connection, with no recovery needed."""
    if not _online():
        pytest.skip("no internet connection")
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    sequence = ["https://github.com", "https://wikipedia.org", "https://example.com"]
    try:
        for _ in range(5):
            for url in sequence:
                result = browser.navigate(url)
                assert arrived(url, result.url), (url, result.url)
                assert result.attempts == 1
        assert browser.reconnects == 0
        assert [e["event"] for e in _events(browser)] == ["connected"]
    finally:
        assert browser.stop()

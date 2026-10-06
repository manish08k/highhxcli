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
from typing import Any

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
from tests.unit.agent.conftest import RecordingUI, agent_project, make_app  # noqa: F401

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


def _open_fds() -> int:
    return len(list(Path("/dev/fd").iterdir()))


def test_repeated_start_use_stop_cycles_leak_no_process_socket_or_file(site: str, tmp_path: Path) -> None:
    from highhx.computer import browser as browser_module
    from highhx.computer.browser_sessions import BrowserSessionManager

    def cycle(i: int) -> int:
        browser = ChromeBrowser(tmp_path / "state", headless=True)
        try:
            browser.navigate(f"{site}/index.html?i={i}")
            assert browser.observe().title == "Shop"
            pid = int((browser._state() or {})["pid"])
        finally:
            assert browser.stop()
        return pid

    cycle(0)  # warm-up: imports, caches and the first profile write are not leaks
    fds = _open_fds()
    pids = [cycle(i) for i in range(1, 6)]
    assert not any(browser_module._alive(pid) for pid in pids)  # every browser exited and was reaped
    assert not set(pids) & set(browser_module._LAUNCHED)
    assert _open_fds() <= fds + 2, (fds, _open_fds())  # no socket, pipe or file left per cycle

    manager = BrowserSessionManager(tmp_path / "managed", headless=True)
    session = manager.start(profile="default")
    try:
        manager.screenshot(session.id)
        fds = _open_fds()
        for _ in range(10):  # each call used to leave a DevTools socket open
            assert manager.screenshot(session.id)[:4] == b"\x89PNG"
            assert manager.heartbeat(session.id).healthy
        assert _open_fds() <= fds + 2, (fds, _open_fds())
    finally:
        manager.stop(session.id)


KEYS_PAGE = (
    "<!doctype html><title>Keys</title>"
    "<input id=a aria-label=First><input id=b aria-label=Second><textarea id=t aria-label=Notes></textarea>"
    "<p id=log></p><script>"
    "const seen=[];document.addEventListener('keydown',e=>{seen.push([e.key,e.code,e.shiftKey,e.ctrlKey,e.altKey,e.metaKey]);"
    "log.textContent=JSON.stringify(seen.slice(-1))});window.seen=seen;</script>"
)


def test_keyboard_keys_combos_and_editing_in_a_real_browser(site: str, tmp_path: Path) -> None:
    import json as _json
    import sys as _sys

    (tmp_path / "keys.html").write_text(KEYS_PAGE)
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    mod = "cmd" if _sys.platform == "darwin" else "ctrl"

    def js(expr: str) -> Any:
        return browser._eval(expr, None)

    def last_key() -> list[Any]:
        return list(_json.loads(js("JSON.stringify(window.seen[window.seen.length-1])")))

    try:
        browser.navigate(f"{site}/keys.html")
        first = next(e for e in browser.observe().elements if e.name == "First")
        browser.type_text(first.id, "hello world")
        # every named key reaches the page as the key it names
        named = {
            "enter": "Enter",
            "tab": "Tab",
            "escape": "Escape",
            "backspace": "Backspace",
            "forwarddelete": "Delete",
            "arrowleft": "ArrowLeft",
            "arrowright": "ArrowRight",
            "arrowup": "ArrowUp",
            "arrowdown": "ArrowDown",
            "home": "Home",
            "end": "End",
            "pageup": "PageUp",
            "pagedown": "PageDown",
            "space": " ",
            "f5": "F5",
        }
        for key, dom in named.items():
            js("document.getElementById('a').focus()")
            browser.press(key)
            assert last_key()[0] == dom, (key, last_key())
        # modifiers arrive as modifiers
        js("document.getElementById('a').focus()")
        browser.press("shift+arrowleft")
        assert last_key()[:3] == ["ArrowLeft", "ArrowLeft", True]
        browser.press("ctrl+alt+k")
        assert last_key()[3:5] == [True, True] and last_key()[1] == "KeyK"
        # editing shortcuts do what they do for a person (macOS: cmd, through Chrome's editing commands)
        js("const a=document.getElementById('a');a.value='hello world';a.focus();a.setSelectionRange(11,11)")
        browser.press(f"{mod}+a")
        assert js("[a.selectionStart,a.selectionEnd].join()") == "0,11"
        browser.press("backspace")
        assert js("a.value") == ""
        browser.insert_text("abc")
        browser.press("arrowleft")
        browser.press("forwarddelete")
        assert js("a.value") == "ab"
        browser.press(f"{mod}+z")
        assert js("a.value") == "abc"
        browser.press(f"{mod}+shift+z")
        assert js("a.value") == "ab"  # redo
        js("a.value='ab';a.setSelectionRange(2,2)")
        browser.press("shift+a")  # a printable key with shift types the shifted character
        browser.press("shift+1")
        browser.press("7")
        assert js("a.value") == "abA!7"
        if mod == "cmd":  # macOS line keys (Home/End scroll there)
            browser.press("cmd+arrowleft")
            browser.insert_text("<")
            browser.press("cmd+arrowright")
            browser.insert_text(">")
        else:
            browser.press("home")
            browser.insert_text("<")
            browser.press("end")
            browser.insert_text(">")
        assert js("a.value") == "<abA!7>"
        # focus: Tab and Shift+Tab move between fields
        js("document.getElementById('a').focus()")
        browser.press("tab")
        assert js("document.activeElement.id") == "b"
        browser.press("shift+tab")
        assert js("document.activeElement.id") == "a"
        # Enter in a textarea is a line break, not a submit
        js("document.getElementById('t').focus()")
        browser.insert_text("one")
        browser.press("enter")
        browser.insert_text("two")
        assert js("document.getElementById('t').value") == "one\ntwo"
    finally:
        assert browser.stop()


PRECISION_PAGE = (
    "<!doctype html><title>Targets</title><style>body{margin:0;width:3000px;height:3000px}"
    ".t{position:absolute;width:8px;height:8px;border:0;padding:0}</style>"
    "<p id=hit>none</p>"
    "<button class=t id=near style='left:1500px;top:1200px;background:#00ff00' onclick=\"hit.textContent='near'\"></button>"
    "<button class=t id=next style='left:1510px;top:1200px;background:#0000ff' onclick=\"hit.textContent='next'\"></button>"
    "<iframe id=f style='position:absolute;left:1600px;top:1300px;width:200px;height:120px;border:0' srcdoc=\""
    "<body style=margin:0><button style='position:absolute;left:50px;top:40px;width:8px;height:8px;border:0;"
    "padding:0;background:#ff00ff' onclick=&quot;parent.document.getElementById('hit').textContent='frame'&quot;>"
    '</button></body>"></iframe>'
)


def test_screenshot_pixels_are_click_points_under_scroll_zoom_dpr_and_frames(site: str, tmp_path: Path) -> None:
    from highhx.perception.png import decode

    (tmp_path / "precision.html").write_text(PRECISION_PAGE)
    browser = ChromeBrowser(tmp_path / "state", headless=True)

    def find(color: tuple[int, int, int]) -> tuple[int, int]:
        data, _view = browser.page_capture()
        image = decode(data)
        points = [(x, y) for y in range(image.height) for x in range(image.width) if image.pixel(x, y) == color]
        assert points, f"{color} is not on screen"
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2

    def click(color: tuple[int, int, int], expected: str, element: str) -> None:
        browser._eval(f"hit.textContent='none';{element}.scrollIntoView({{block:'center',inline:'center'}})", None)
        time.sleep(0.2)
        x, y = find(color)
        browser.pointer("click", x, y)
        assert browser._eval("hit.textContent", None) == expected, (color, (x, y), browser.viewport())

    def cdp(method: str, params: dict[str, Any]) -> None:
        from highhx.computer.browser import Retry

        browser._run(method, Retry.SAFE, lambda c, s: c.call(method, params, session_id=s), None)

    def conditions() -> Iterator[str]:
        yield "scrolled"
        cdp(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1000, "height": 700, "deviceScaleFactor": 2, "mobile": False},
        )
        yield "device pixel ratio 2"
        browser._eval("document.body.style.zoom='1.5'", None)
        yield "page zoom 150%"
        browser._eval("document.body.style.zoom='1'", None)
        cdp("Emulation.setPageScaleFactor", {"pageScaleFactor": 2})
        yield "pinch zoom 2x"

    try:
        browser.navigate(f"{site}/precision.html")
        for condition in conditions():
            targets = (((0, 255, 0), "near", "near"), ((0, 0, 255), "next", "next"), ((255, 0, 255), "frame", "f"))
            for color, expected, element in targets:
                try:
                    click(color, expected, element)
                except AssertionError as exc:
                    raise AssertionError(f"{condition}: {exc}") from None
    finally:
        assert browser.stop()


SCROLL_PAGE = (
    "<!doctype html><title>Wide</title><style>body{margin:0;width:4000px;height:3000px}</style>"
    "<div id=panel style='position:absolute;left:20px;top:60px;width:200px;height:120px;overflow:auto'>"
    "<div style='width:1000px;height:1000px'>panel</div></div>"
    "<button id=go style='position:absolute;left:400px;top:300px;width:30px;height:20px;background:#00ff00;border:0'"
    " onclick=\"this.textContent='hit'\"></button>"
)


def test_capture_clicks_and_scrolling(site: str, tmp_path: Path, agent_project: Path, make_app: Any) -> None:  # noqa: F811
    from highhx.actions.executor import ActionExecutor
    from highhx.computer.session import ComputerSession

    (tmp_path / "wide.html").write_text(SCROLL_PAGE)
    app = make_app(agent_project)
    gate = ActionGate(
        app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor)
    )
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state", headless=True)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    try:
        assert executor.run("browser.open", {"url": f"{site}/wide.html"}).ok
        browser = session.browser
        shot = executor.run("browser.screenshot", {})
        assert shot.ok and shot.output["capture"]
        # the green button's centre in the capture, by pixel: (415, 310) CSS = (415, 310) image
        clicked = executor.run("browser.click_at", {"x": 415, "y": 310, "capture": shot.output["capture"]})
        assert clicked.ok and browser._eval("go.textContent", None) == "hit"
        used = executor.run("browser.click_at", {"x": 415, "y": 310, "capture": shot.output["capture"]})
        assert not used.ok and "Stale screenshot" in used.error  # an action since: the capture is retired
        # the page scrolls by itself after a fresh capture: its pixels no longer show what is there
        fresh = executor.run("browser.screenshot", {}).output["capture"]
        browser._eval("scrollTo(0, 40)", None)
        stale = executor.run("browser.click_at", {"x": 415, "y": 310, "capture": fresh})
        assert not stale.ok and "scrolled" in stale.error
        browser._eval("scrollTo(0, 0)", None)
        assert executor.run("browser.scroll", {"direction": "right"}).ok
        assert float(browser._eval("scrollX", None)) > 0  # horizontal page scroll
        assert executor.run("browser.scroll", {"direction": "left"}).ok
        assert float(browser._eval("scrollX", None)) == 0
        # wheel input at a point scrolls the panel under it, not the page
        for direction, axis in (("down", "scrollTop"), ("right", "scrollLeft")):
            result = executor.run("browser.scroll", {"direction": direction, "x": 100, "y": 110})
            assert result.ok and result.verified, result.error
            assert float(browser._eval(f"panel.{axis}", None)) > 0
        assert float(browser._eval("scrollY", None)) == 0 and float(browser._eval("scrollX", None)) == 0
        # keys through the executor: every key the CLI and flows offer, and combinations
        browser._eval(
            "document.body.insertAdjacentHTML('beforeend','<input id=k aria-label=K>');k.focus();k.value='abc'", None
        )
        for key in ("backspace", "arrowleft", "forwarddelete", "home", "shift+tab", "pagedown", "f5"[:0] or "escape"):
            result = executor.run("browser.press", {"key": key})
            assert result.ok, (key, result.error)
        assert browser._eval("k.value", None) == "a"  # "abc" → backspace → "ab" → left, forward delete → "a"
    finally:
        if session._browser is not None:
            session._browser.stop()
        executor.close()
        session.close()


def test_the_page_in_front_is_known_without_asking_the_page(site: str, tmp_path: Path) -> None:
    # host policy rules read this: it must follow a link the page itself took
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        assert browser.known_url() == ""  # nothing started, nothing known
        browser.navigate(f"{site}/links.html")
        assert browser.known_url() == f"{site}/links.html"
        browser.evaluate("location.href = 'results.html?q=moved'", retry_safe=False)
        browser.wait_ready()
        deadline = time.monotonic() + 5
        while "results.html" not in browser.known_url() and time.monotonic() < deadline:
            browser.pump_events(0.1)
        assert browser.known_url() == f"{site}/results.html?q=moved"
    finally:
        assert browser.stop()


SECRETS_PAGE = (
    "<!doctype html><title>Pay</title><style>body{margin:0;background:#fff} input{position:absolute;width:200px;"
    "height:30px;font:20px monospace;border:0;background:#fff;color:#000}</style>"
    "<input id=pw type=text autocomplete=current-password value=hunter2-shown style='left:20px;top:20px'>"
    "<input id=cc autocomplete=cc-number value=4111111111111111 style='left:20px;top:80px'>"
    "<input id=otp autocomplete=one-time-code value=123456 style='left:20px;top:140px'>"
    "<input id=name aria-label=Name value=Ada style='left:20px;top:200px'>"
)


def test_screenshots_black_out_secret_fields(site: str, tmp_path: Path) -> None:
    from highhx.computer.capture import CaptureStore
    from highhx.perception.png import decode

    (tmp_path / "pay.html").write_text(SECRETS_PAGE)
    browser = ChromeBrowser(tmp_path / "state", headless=True)
    try:
        browser.navigate(f"{site}/pay.html")
        capture = CaptureStore().take_page(browser)
        assert capture.redacted == 3 and capture.to_dict()["redacted"] == 3
        page = decode(capture.shot.path.read_bytes())
        for y in (35, 95, 155):  # the shown password, the card number, the one-time code
            assert {page.pixel(x, y) for x in range(22, 218, 4)} == {(0, 0, 0)}, y
        assert len({page.pixel(x, 215) for x in range(22, 218)}) > 1  # the name field is drawn as it is
        raw = decode(browser.screenshot())  # device pixels
        dpr = raw.width / page.width
        assert {raw.pixel(int(x * dpr), int(95 * dpr)) for x in range(22, 218, 4)} == {(0, 0, 0)}
        capture.shot.path.unlink(missing_ok=True)
    finally:
        assert browser.stop()

"""Real Chrome/Chromium through DevTools. Opt-in: HIGHHX_TEST_BROWSER=1 (needs a browser that
can start in this environment)."""

from __future__ import annotations

import functools
import http.server
import os
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from highhx.commands import App
from highhx.computer.browser import ChromeBrowser, find_browser
from highhx.computer.runtime import ComputerRuntime
from highhx.core.context import Options
from highhx.core.errors import ApprovalDeniedError, NotFoundError
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

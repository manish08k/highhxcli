"""Recording and self-healing replay in a real Chrome. Opt-in: HIGHHX_TEST_BROWSER=1.

A person's click is captured by the recording binding (a real input event through DevTools),
the workflow is saved, the site is redesigned (labels, classes and layout change; the test id
does not), and the replay finds and heals both targets through the real browser runtime and
executor."""

from __future__ import annotations

import functools
import http.server
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.handlers.state import state_from_result
from highhx.computer.browser import find_browser
from highhx.computer.recorder import BrowserRecorder, WorkflowStore, replay
from highhx.computer.session import ComputerSession
from highhx.perception.png import png_size
from highhx.safety.actions import Actor
from highhx.safety.audit import AuditLog
from highhx.safety.gate import ActionGate, ApprovalMode
from tests.unit.agent.conftest import RecordingUI

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)

V1 = {
    "index.html": "<!doctype html><title>Shop</title><a class='nav nav-invoices' href='/invoices.html'>Invoices</a>",
    "invoices.html": "<!doctype html><title>Invoices</title><h1>Invoices</h1><p id=s></p>"
    "<button data-testid=export-invoices onclick=\"s.textContent='Export ready'\">Export</button>",
}
V2 = {
    "index.html": "<!doctype html><title>Shop</title><div style='margin:80px'><a href='/promo.html'>Promotions</a> "
    "<a class='css-1q2w3e' href='/invoices.html'>Billing documents</a></div>",
    "invoices.html": "<!doctype html><title>Invoices</title><h1>Invoices</h1><p id=s></p><div style='margin-left:300px'>"
    "<button class='btn-x9' data-testid=export-invoices onclick=\"s.textContent='Export ready'\">Download CSV</button></div>",
}


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "site"
    root.mkdir()
    for name, html in V1.items():
        (root / name).write_text(html)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", root
    finally:
        server.shutdown()
        server.server_close()


def test_record_then_heal_after_a_redesign(site: tuple[str, Path], agent_project: Path, make_app, tmp_path: Path) -> None:
    base, root = site
    app = make_app(agent_project)
    gate = ActionGate(app.engine, RecordingUI(), source="test", mode=ApprovalMode.ASK, audit=AuditLog(app.db, app.redactor))
    session = ComputerSession(gate, actor=Actor.USER, state_dir=tmp_path / "state", headless=True)
    executor = ActionExecutor(app, gate, actor=Actor.USER, computer=lambda: session)
    try:
        browser = session.browser
        assert executor.run("browser.open", {"url": f"{base}/index.html"}).ok  # through the executor
        recorder = BrowserRecorder(browser)
        recorder.start(f"{base}/index.html")
        browser.wait_ready()

        def click(name: str) -> None:  # what a person does: a real mouse click on the element
            element = next(e for e in browser.observe().elements if e.name == name)
            x, y, w, h = element.bounds  # type: ignore[misc]
            browser.pointer("click", x + w / 2, y + h / 2)
            deadline = time.monotonic() + 5
            count = len(recorder.events)
            while len(recorder.events) <= count and time.monotonic() < deadline:
                recorder.poll(0.2)
            browser.wait_ready()

        click("Invoices")
        click("Export")
        recorder.poll(0.5)
        recorder.stop()
        workflow = recorder.workflow("export", f"{base}/index.html")
        assert [s.action for s in workflow.steps] == ["click", "click"], recorder.events
        assert workflow.steps[0].verify == {"url_contains": "/invoices.html"}
        assert workflow.steps[1].target["dom"]["testid"] == "export-invoices"
        store = WorkflowStore(tmp_path / "workflows")
        store.save(workflow)

        shot = state_from_result(executor.run("computer.state", {"surface": "browser", "screenshot": True}))
        assert shot is not None and shot.screenshot is not None and png_size(Path(shot.screenshot.path).read_bytes())[0] > 100

        for name, html in V2.items():
            (root / name).write_text(html)
        from highhx.trajectories import TrajectoryStore

        trajectories = TrajectoryStore(tmp_path / "trajectories")
        report = replay(executor, store.load("export"), workflows=store, trajectories=trajectories)
        assert report.status == "completed", trajectories.load(report.task_id).describe() + str([s.result.get("error") for s in trajectories.load(report.task_id).steps])
        assert [(h["was"], h["now"]) for h in report.healed] == [("Invoices", "Billing documents"), ("Export", "Download CSV")]
        assert all(h["strategy"] == "dom" for h in report.healed) and report.saved
        assert "Export ready" in browser.observe().text
    finally:
        session.close()

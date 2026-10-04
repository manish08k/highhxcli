"""A benchmark task in the real HighhX browser (Chrome over DevTools): the metrics come from a
real execution. Opt-in: HIGHHX_TEST_BROWSER=1."""

from __future__ import annotations

import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

from highhx.benchmarks import BenchmarkRunner, BenchmarkTask
from highhx.computer.browser import find_browser

pytestmark = pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)


def test_a_real_browser_task_is_measured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>Invoices</title><p id=s></p>"
        "<button type=button data-testid=export onclick=\"s.textContent='Export ready'\">Export</button>"
    )
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *a: None  # type: ignore[attr-defined]
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("HIGHHX_HEADLESS", "1")
    url = f"http://127.0.0.1:{server.server_address[1]}/index.html"
    task = BenchmarkTask.from_dict(
        {
            "id": "real-export",
            "environment": {"kind": "browser"},
            "planner": {
                "kind": "scripted",
                "steps": [
                    {"action": "open", "parameters": {"url": url}},
                    {"action": "click", "target": {"label": "Export", "role": "button"}},
                ],
            },
            "success": {"text": "Export ready"},
            "ground_truth": {"Export": "Export"},
            "risk_level": "medium",
        }
    )
    try:
        metrics = BenchmarkRunner().run_task(task)
    finally:
        server.shutdown()
        server.server_close()
    assert metrics.success and metrics.status == "completed", metrics.summary
    assert metrics.grounding_accuracy == 1.0 and metrics.verification_accuracy == 1.0
    assert metrics.tool_cost >= 4 and metrics.completion_time > 0

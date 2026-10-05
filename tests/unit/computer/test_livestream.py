"""Live viewing: the frame broker's rate cap and waiting, the screenshot poller (desktop), and the
browser screencast reconnecting after its tab crashes (real Chrome: HIGHHX_TEST_BROWSER=1)."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import pytest

from highhx.computer.browser import find_browser
from highhx.computer.livestream import BrowserScreencast, FrameBroker, ScreenshotPoller


def test_the_broker_caps_the_frame_rate_and_wakes_waiters() -> None:
    broker = FrameBroker(max_fps=5)
    assert broker.publish(b"a", "image/png", "t") and not broker.publish(b"b", "image/png", "t")  # 2nd within 200 ms
    assert broker.stats()["dropped"] == 1 and broker.latest().data == b"a"
    got: list[bytes] = []
    waiter = threading.Thread(target=lambda: got.append(broker.wait_next(1, timeout=3).data))
    waiter.start()
    time.sleep(0.25)
    broker.publish(b"c", "image/png", "t")
    waiter.join(3)
    assert got == [b"c"] and broker.wait_next(2, timeout=0.05) is None


def test_the_poller_streams_and_survives_failed_captures() -> None:
    calls = {"n": 0}

    def capture() -> bytes:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("screen locked")
        return b"png%d" % calls["n"]

    broker = FrameBroker(max_fps=0)
    poller = ScreenshotPoller(capture, broker, fps=2.0)
    poller.start()
    deadline = time.monotonic() + 4
    while broker.stats()["frames"] < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    poller.stop()
    assert broker.stats()["frames"] >= 2 and calls["n"] >= 3  # the failed capture did not stop it
    assert ScreenshotPoller(capture, broker, fps=50).interval == 0.5  # desktop polling is capped at 2 fps


@pytest.mark.skipif(
    not (os.environ.get("HIGHHX_TEST_BROWSER") and find_browser()),
    reason="set HIGHHX_TEST_BROWSER=1 to drive a real browser",
)
def test_the_screencast_comes_back_after_the_browser_restarts(tmp_path: Path) -> None:
    from highhx.computer.browser import ChromeBrowser

    browser = ChromeBrowser(tmp_path, headless=True)
    broker = FrameBroker(max_fps=0)
    stream = BrowserScreencast(browser, broker, quality=40, max_width=640)
    try:
        browser.navigate("data:text/html,<h1>one</h1>")
        stream.start()
        assert broker.wait_next(0, timeout=15) is not None
        os.kill(int(browser._saved()["pid"]), signal.SIGKILL)  # the browser dies under the stream
        time.sleep(0.5)
        browser.navigate("data:text/html,<h1>two</h1>")  # HighhX recovers it
        seen = broker.latest().seq
        browser.evaluate("document.body.style.background='red'", retry_safe=True)
        assert broker.wait_next(seen, timeout=20) is not None and stream.restarts >= 1
    finally:
        stream.stop()
        browser.stop()

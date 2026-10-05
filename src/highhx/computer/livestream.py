"""Live viewing: frames from a browser or the desktop, to viewers.

    source (browser screencast · desktop screenshots)  →  FrameBroker (latest frame, fps cap)  →  viewers

- **Browser**: Chrome's own screencast (``Page.startScreencast``: JPEG, a quality and a maximum
  size), on a *separate* DevTools connection to the tab HighhX works in, so streaming never reads
  from the connection actions use. It follows tab switches and reconnects after a lost connection
  or a crashed tab (with backoff).
- **Desktop**: screenshots through the computer driver, polled at a low rate (≤ 2 fps).

Controls: ``max_fps`` (the broker drops frames above it), ``quality`` and ``max_width`` (bandwidth).
Privacy: frames live in memory only (the latest one), are never written to trajectories, logs or
benchmarks, and are served only by the authenticated loopback console (``highhx web``). Pages mask
password fields themselves; the console shows what the person could see on their own screen.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Frame:
    seq: int
    data: bytes
    mime: str
    captured: float
    source: str


class FrameBroker:
    """The latest frame of one source, with a frame-rate cap and counters."""

    def __init__(self, *, max_fps: float = 10.0) -> None:
        self.max_fps = max_fps
        self._frame: Frame | None = None
        self._seq = 0
        self._cond = threading.Condition()
        self.frames = 0
        self.dropped = 0
        self.bytes = 0
        self._last = 0.0

    def publish(self, data: bytes, mime: str, source: str) -> bool:
        now = time.monotonic()
        with self._cond:
            if self.max_fps > 0 and now - self._last < 1.0 / self.max_fps:
                self.dropped += 1
                return False
            self._last = now
            self._seq += 1
            self._frame = Frame(self._seq, data, mime, time.time(), source)
            self.frames += 1
            self.bytes += len(data)
            self._cond.notify_all()
        return True

    def latest(self) -> Frame | None:
        with self._cond:
            return self._frame

    def wait_next(self, after: int, timeout: float = 5.0) -> Frame | None:
        """The first frame newer than ``after`` (None after ``timeout``)."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._frame is None or self._frame.seq <= after:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)
            return self._frame

    def stats(self) -> dict[str, Any]:
        with self._cond:
            return {
                "frames": self.frames,
                "dropped": self.dropped,
                "bytes": self.bytes,
                "seq": self._seq,
                "max_fps": self.max_fps,
            }


class _Streamer:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error = ""
        self.restarts = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=type(self).__name__, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError


class BrowserScreencast(_Streamer):
    """Chrome's screencast of the tab ``browser`` works in, into ``broker``."""

    def __init__(
        self, browser: Any, broker: FrameBroker, *, quality: int = 60, max_width: int = 1280, max_height: int = 900
    ) -> None:
        super().__init__()
        self.browser = browser
        self.broker = broker
        self.quality = max(10, min(100, quality))
        self.max_width = max_width
        self.max_height = max_height
        self.target = ""

    def _page_url(self) -> tuple[str, str]:
        port = int(self.browser._saved().get("port") or 0)
        if not port:
            raise ConnectionError("the browser is not running")
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:  # nosec B310 - local DevTools
            targets = json.loads(r.read())
        wanted = self.browser._target_id
        pages = [t for t in targets if t.get("type") == "page"]
        chosen = next((t for t in pages if t.get("id") == wanted), pages[0] if pages else None)
        if chosen is None:
            raise ConnectionError("the browser has no page")
        return str(chosen["id"]), str(chosen["webSocketDebuggerUrl"])

    def _run(self) -> None:
        from highhx.computer.cdp import CDPConnection

        backoff = 0.5
        while not self._stop.is_set():
            conn = None
            try:
                target, url = self._page_url()
                conn = CDPConnection(url, target_id=target)
                self.target = target

                def on_event(message: dict[str, Any], conn: Any = conn) -> None:
                    if message.get("method") != "Page.screencastFrame":
                        return
                    params = message.get("params") or {}
                    with contextlib.suppress(Exception):
                        conn.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")})
                    self.broker.publish(base64.b64decode(str(params.get("data") or "")), "image/jpeg", "browser")

                conn.listeners.append(on_event)
                conn.call("Page.enable", timeout=10)
                conn.call(
                    "Page.startScreencast",
                    {
                        "format": "jpeg",
                        "quality": self.quality,
                        "maxWidth": self.max_width,
                        "maxHeight": self.max_height,
                    },
                    timeout=10,
                )
                self.error, backoff = "", 0.5
                while not self._stop.is_set() and conn.usable:
                    conn.pump(seconds=0.5)
                    if self.browser._target_id and self.browser._target_id != target:
                        break  # HighhX switched tabs: follow it
            except Exception as exc:
                self.error = str(exc)[:200]
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)
            finally:
                if conn is not None:
                    with contextlib.suppress(Exception):
                        conn.call("Page.stopScreencast", timeout=2)
                    conn.close()
                    if not self._stop.is_set():
                        self.restarts += 1


class ScreenshotPoller(_Streamer):
    """Frames from ``capture()`` (PNG bytes) at ``fps`` — the desktop, or a browser without screencast."""

    def __init__(
        self, capture: Callable[[], bytes], broker: FrameBroker, *, fps: float = 1.0, source: str = "desktop"
    ) -> None:
        super().__init__()
        self.capture = capture
        self.broker = broker
        self.interval = 1.0 / max(0.1, min(fps, 2.0))
        self.source = source

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                data = self.capture()
                if data:
                    self.broker.publish(data, "image/png", self.source)
                self.error = ""
            except Exception as exc:
                self.error = str(exc)[:200]
            self._stop.wait(max(0.0, self.interval - (time.monotonic() - started)))

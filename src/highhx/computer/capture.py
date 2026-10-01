"""Screenshots a model can point into — and the checks that make its coordinates safe to use.

A vision model sees an image and answers with a pixel (``click(640, 212)``) or a position on a
0–1000 grid. :class:`CaptureStore` keeps, for each screenshot it hands out, what the image showed:
where its top-left pixel is on the desktop, how many pixels make one point (Retina, downscaling),
the windows front to back, and the screen's size. A coordinate becomes a desktop point only when

- it names the **newest** capture — an older image may show a UI that is gone;
- that capture is **fresh** (two minutes) and **unused**: an action that changed the desktop
  (click, keys, drag …) retires it, so the model looks again before acting again;
- it lies **inside** the image;
- the screen has the same size and scale;
- the window under the point is still the one that was there, unmoved and uncovered.

Anything else is a :class:`StaleCapture` error — the caller takes a new screenshot and decides
again. Nothing here acts on the computer: it hands a verified desktop point to the action that
asked for it.
"""

from __future__ import annotations

import base64
import itertools
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver, Screenshot, Window

MAX_AGE = 120.0
"""Seconds a screenshot's coordinates stay usable."""
MODEL_MAX_SIZE = 1280
"""Long side of screenshots made for a model (sharp enough to read, small enough to send)."""
FRAME_SLACK = 4
SPACES = ("pixels", "relative1000")


class StaleCapture(HighhXError):
    """The screenshot a coordinate refers to no longer shows the desktop: look again."""


@dataclass
class Capture:
    id: str
    shot: Screenshot
    windows: list[Window]
    """The windows front to back when it was taken."""
    screen: tuple[int, int, float]
    """(width, height, scale) of the main display then."""
    target: str
    """What it shows: "the screen", "window 4211 (TextEdit)", "region …"."""
    taken: float = field(default_factory=time.monotonic)
    retired: str = ""
    """Why it may no longer be used (an action changed the desktop after it)."""
    page: dict[str, Any] | None = None
    """For a browser page's capture: the URL, scroll offset and viewport it showed (CSS pixels)."""

    def label(self) -> str:
        return (
            f"screenshot {self.id} of {self.target}: {self.shot.width}x{self.shot.height} pixels "
            f"(pixel (0, 0) is desktop point {self.shot.origin}; {self.shot.scale:.3g} pixels per point)"
        )

    def image_data(self) -> str:
        return base64.b64encode(self.shot.path.read_bytes()).decode("ascii")

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture": self.id,
            "path": str(self.shot.path),
            "width": self.shot.width,
            "height": self.shot.height,
            "origin": list(self.shot.origin),
            "scale": self.shot.scale,
            "target": self.target,
        }


def window_at(windows: list[Window], point: tuple[int, int]) -> Window | None:
    x, y = point
    return next((w for w in windows if w.x <= x < w.x + w.width and w.y <= y < w.y + w.height), None)


def _moved(a: Window, b: Window) -> bool:
    pairs = zip((a.x, a.y, a.width, a.height), (b.x, b.y, b.width, b.height), strict=True)
    return max(abs(p - q) for p, q in pairs) > FRAME_SLACK


class CaptureStore:
    """The screenshots of one computer session (shared by its actions, the agent and MCP)."""

    def __init__(self) -> None:
        self._items: dict[str, Capture] = {}
        self._ids = itertools.count(1)
        self.latest: str | None = None

    def take(
        self,
        driver: HighhXDriver,
        *,
        window: int | None = None,
        region: tuple[int, int, int, int] | None = None,
        max_size: int | None = MODEL_MAX_SIZE,
    ) -> Capture:
        shot = driver.screenshot(window, region=region, max_size=max_size)
        windows = driver.windows()
        screen = driver.screen()
        if window is not None:
            owner = next((w for w in windows if w.id == window), None)
            target = f"window {window}" + (f" ({owner.app})" if owner else "")
        elif region is not None:
            target = f"region {list(region)}"
        else:
            target = "the screen"
        capture = Capture(f"c{next(self._ids)}", shot, windows, (screen.width, screen.height, screen.scale), target)
        self._items[capture.id] = capture
        self.latest = capture.id
        for old in list(self._items)[:-3]:  # a few recent ones for history; older files are removed
            self._items.pop(old).shot.path.unlink(missing_ok=True)
        return capture

    def get(self, capture_id: str) -> Capture:
        found = self._items.get(capture_id)
        if found is None:
            raise StaleCapture(f"There is no screenshot {capture_id!r} (any more); take a new one.")
        return found

    def retire(self, reason: str) -> None:
        """An action changed the desktop: no earlier screenshot's coordinates may be used again."""
        for capture in self._items.values():
            capture.retired = capture.retired or reason

    def _pixel(self, capture: Capture, x: float, y: float, space: str) -> tuple[int, int]:
        """The rules every screenshot coordinate meets: newest, unused, fresh, inside the image."""
        if capture.id != self.latest:
            raise StaleCapture(f"Screenshot {capture.id} is older than {self.latest}: use the newest screenshot.")
        if capture.retired:
            raise StaleCapture(f"Screenshot {capture.id} was taken before {capture.retired}; take a new one.")
        if time.monotonic() - capture.taken > MAX_AGE:
            raise StaleCapture(f"Screenshot {capture.id} is more than {MAX_AGE:.0f} seconds old; take a new one.")
        if space not in SPACES:
            raise StaleCapture(f"Unknown coordinate space {space!r} (use {' or '.join(SPACES)}).")
        width, height = capture.shot.width, capture.shot.height
        px, py = (x * width / 1000, y * height / 1000) if space == "relative1000" else (x, y)
        if not (0 <= px < width and 0 <= py < height):
            raise StaleCapture(f"({x:g}, {y:g}) is outside screenshot {capture.id} ({width}x{height} pixels).")
        return capture.shot.to_point(px, py)

    def take_page(self, browser: Any, *, cancel: Any = None) -> Capture:
        """The browser page's viewport as a screenshot (one pixel per CSS pixel)."""
        from pathlib import Path

        from highhx.automation.engine.platforms import png_size
        from highhx.computer.driver import Screenshot
        from highhx.utils.paths import user_data_dir

        data, view = browser.page_capture(cancel=cancel)
        folder = user_data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        number = next(self._ids)
        path = Path(folder / f"page-{number}-{int(time.time())}.png")
        path.write_bytes(data)
        width, height = png_size(path)
        view_width = float(view.get("width") or width)
        shot = Screenshot(path, width, height, width / view_width if view_width else 1.0)
        page = {k: view.get(k) for k in ("url", "x", "y", "width", "height")}
        target = f"the page {view.get('title') or view.get('url') or ''}".strip()
        capture = Capture(f"c{number}", shot, [], (int(view_width), int(view.get("height") or height), float(view.get("dpr") or 1)), target, page=page)
        self._items[capture.id] = capture
        self.latest = capture.id
        for old in list(self._items)[:-3]:
            self._items.pop(old).shot.path.unlink(missing_ok=True)
        return capture

    def ground_page(
        self, browser: Any, capture_id: str, x: float, y: float, *, space: str = "pixels", cancel: Any = None
    ) -> tuple[int, int]:
        """A viewport point (CSS pixels) for ``(x, y)`` in page screenshot ``capture_id`` — only while
        the page is still the one captured: same URL, scroll position and viewport size."""
        capture = self.get(capture_id)
        if capture.page is None:
            raise StaleCapture(f"Screenshot {capture_id} shows the desktop, not the browser page.")
        point = self._pixel(capture, x, y, space)
        now = browser.viewport(cancel=cancel)
        for key, what in (("url", "the page changed"), ("x", "the page scrolled"), ("y", "the page scrolled"), ("width", "the window resized"), ("height", "the window resized")):
            if now.get(key) != capture.page.get(key):
                raise StaleCapture(f"{what.capitalize()} since screenshot {capture_id}; take a new one.")
        return point

    def ground(
        self, driver: HighhXDriver, capture_id: str, x: float, y: float, *, space: str = "pixels"
    ) -> tuple[int, int]:
        """The desktop point that ``(x, y)`` in screenshot ``capture_id`` shows — only while that
        screenshot still shows the desktop (see the module docstring); else :class:`StaleCapture`."""
        capture = self.get(capture_id)
        point = self._pixel(capture, x, y, space)
        if capture.page is not None:
            raise StaleCapture(f"Screenshot {capture_id} shows a browser page, not the desktop.")
        screen = driver.screen()
        if (screen.width, screen.height, screen.scale) != capture.screen:
            raise StaleCapture("The display changed size or scale since the screenshot; take a new one.")
        then = window_at(capture.windows, point)
        now = window_at(driver.windows(), point)
        if then is not None:
            if now is None or now.id != then.id:
                what = f"covered by {now.app}" if now is not None else "gone"
                raise StaleCapture(f"{then.app or 'The window'} at {list(point)} is {what}; take a new screenshot.")
            if _moved(now, then):
                raise StaleCapture(f"{then.app or 'The window'} moved since screenshot {capture_id}; take a new one.")
        elif now is not None:
            raise StaleCapture(f"{now.app} opened at {list(point)} after the screenshot; take a new one.")
        return point

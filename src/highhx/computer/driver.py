"""The HighhX Computer API: one object for observing and operating a computer.

    from highhx.computer import HighhXDriver

    with HighhXDriver.create() as driver:
        state = driver.observe(screenshot=True)       # active app and window, windows, UI tree
        driver.click(640, 400)                         # or driver.press_element("Save", role="button")
        driver.type_text("hello")
        driver.hotkey("cmd+s")

Layers:

    HighhXDriver            typed operations, sessions, observation        (this module)
      └ AutomationBridge    the protocol: fixed operations, validated arguments, the terminal guard
          └ engine          the built-in engine's platform backend (macOS, Windows, Linux) or the
                            C#/.NET engine — native OS APIs underneath

The driver is mechanism, not policy. Inside HighhX it is always reached through the action
executor (risk classification, approval, audit, verification — :mod:`highhx.actions`), the
computer runtime (per-element safety — :mod:`highhx.computer.runtime`) or a command that
checks approval itself. A program that imports it directly acts as its own user.

Operations are synchronous, like the rest of HighhX (``asyncio.to_thread`` makes any of them
awaitable). Coordinates are desktop points in the platform's global space (``screen()``).
"""

from __future__ import annotations

import itertools
import time
import uuid
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from highhx.automation.engine.bridge import AutomationBridge, EngineError, Runner, open_bridge
from highhx.automation.engine.protocol import FEATURES, MODIFIERS
from highhx.computer.desktop import parse_bounds
from highhx.computer.model import Observation, UIElement
from highhx.core.errors import UsageError
from highhx.execution.cancellation import CancellationToken

if TYPE_CHECKING:
    from highhx.computer.verify import StateCheck

TARGETS = ("local",)
"""Where a driver can run. Sandboxes, cloud computers and virtual machines are not part of this
HighhX build (see docs/COMPUTER_RUNTIME.md); asking for one is an error, never a pretend success."""


@dataclass(frozen=True)
class Screen:
    width: int
    height: int
    x: int = 0
    y: int = 0
    scale: float = 1.0
    """Pixels per point (2.0 on a Retina display): screenshot pixels ÷ scale = desktop points."""


@dataclass(frozen=True)
class Screenshot:
    path: Path
    width: int
    height: int
    scale: float = 1.0
    window: int | None = None


@dataclass(frozen=True)
class Window:
    id: int
    pid: int
    app: str
    title: str
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.width // 2, self.y + self.height // 2

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Window:
        return cls(
            int(data.get("id") or 0),
            int(data.get("pid") or 0),
            str(data.get("app") or ""),
            str(data.get("title") or ""),
            int(data.get("x") or 0),
            int(data.get("y") or 0),
            int(data.get("width") or 0),
            int(data.get("height") or 0),
        )


@dataclass(frozen=True)
class App:
    name: str
    pid: int
    bundle_id: str = ""
    frontmost: bool = False


@dataclass(frozen=True)
class Feature:
    name: str
    available: bool
    detail: str


@dataclass
class DesktopState:
    """What the computer shows now — structure first, pixels only when asked for."""

    app: str
    title: str
    window: Window | None
    windows: list[Window]
    tree: Observation | None = None
    screenshot: Screenshot | None = None
    captured_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app": self.app,
            "title": self.title,
            "window": self.window.__dict__ if self.window else None,
            "windows": [w.__dict__ for w in self.windows],
            "elements": [e.to_dict() | {"bounds": list(e.bounds) if e.bounds else None} for e in self.tree.elements]
            if self.tree
            else None,
            "screenshot": {**self.screenshot.__dict__, "path": str(self.screenshot.path)} if self.screenshot else None,
        }


@dataclass
class Session:
    """One driver's lifecycle (Cua's ``start_session`` … ``end_session``): content-free facts only."""

    id: str
    target: str
    engine: str
    started: float = field(default_factory=time.time)
    actions: int = 0
    last_action: str = ""
    ended: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "engine": self.engine,
            "started": self.started,
            "actions": self.actions,
            "last_action": self.last_action,
            "active": self.ended is None,
        }


_LIVE: weakref.WeakValueDictionary[str, HighhXDriver] = weakref.WeakValueDictionary()
_SHOTS = itertools.count(1)
READ_ONLY = frozenset(
    {
        "status",
        "capabilities",
        "screen",
        "apps",
        "windows",
        "cursor",
        "frontmost",
        "running",
        "inspect",
        "element_at",
        "verify",
        "clipboard_read",
        "wait",
    }
)


def parse_hotkey(keys: str | list[str]) -> tuple[list[str], str]:
    """ "cmd+shift+t" / ["cmd", "t"] → (["cmd", "shift"], "t")."""
    parts = keys if isinstance(keys, list) else [p for p in keys.replace(" ", "+").split("+") if p]
    parts = [str(p).strip().lower() for p in parts if str(p).strip()]
    if not parts:
        raise UsageError("no key given")
    modifiers, key = parts[:-1], parts[-1]
    unknown = [m for m in modifiers if m not in MODIFIERS]
    if unknown:
        raise UsageError(f"unknown modifier(s): {', '.join(unknown)} (use cmd, ctrl, alt/option, shift)")
    return modifiers, key


def menu_path(path: str | list[str]) -> list[str]:
    """ "File > Save As…" → ["File", "Save As…"]."""
    items = path if isinstance(path, list) else [p.strip() for p in path.split(">")]
    items = [str(p).strip() for p in items if str(p).strip()]
    if not items:
        raise UsageError("give a menu path, e.g. File > Save")
    return items


def standalone_runner(cancel: CancellationToken | None = None) -> Runner:
    """Runs the engine's fixed argv directly (for programs using the API outside a HighhX command)."""
    from highhx.computer.desktop import run_cancellable

    def run(argv: list[str], what: str) -> tuple[int, str, str]:
        return run_cancellable(argv, timeout=60, cancel=cancel, what=what)

    return run


class HighhXDriver:
    def __init__(self, bridge: AutomationBridge, *, target: str = "local") -> None:
        self.bridge = bridge
        self.session = Session(f"hx-{uuid.uuid4().hex[:12]}", target, bridge.name)
        _LIVE[self.session.id] = self

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def create(
        cls,
        *,
        target: str = "local",
        runner: Runner | None = None,
        cancel: CancellationToken | None = None,
    ) -> HighhXDriver:
        """A driver for this computer (``target="local"``) on the configured engine."""
        if target not in TARGETS:
            raise UsageError(
                f"Unknown computer target {target!r}: this HighhX build operates the local computer only.",
                hint="Sandboxes, cloud computers and virtual machines are not available yet.",
            )
        return cls(open_bridge(runner or standalone_runner(cancel), cancel=cancel), target=target)

    @staticmethod
    def list_sessions() -> list[Session]:
        return sorted((d.session for d in list(_LIVE.values()) if d.session.ended is None), key=lambda s: s.started)

    @staticmethod
    def get_session(session_id: str) -> Session | None:
        driver = _LIVE.get(session_id)
        return driver.session if driver is not None else None

    def end_session(self) -> None:
        if self.session.ended is None:
            self.session.ended = time.time()
            self.bridge.close()
            _LIVE.pop(self.session.id, None)

    close = end_session

    def __enter__(self) -> HighhXDriver:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.end_session()

    @property
    def name(self) -> str:
        return self.bridge.name

    # ------------------------------------------------------------- plumbing
    def call(self, op: str, **args: Any) -> dict[str, Any]:
        """One protocol operation (validated and guarded by the bridge)."""
        if self.session.ended is not None:
            raise UsageError("This computer session has ended.")
        args = {k: v for k, v in args.items() if v is not None}
        result = self.bridge.call(op, **args)
        if op not in READ_ONLY:
            self.session.actions += 1
            self.session.last_action = op
        return result

    # ----------------------------------------------------------- capabilities
    def status(self) -> dict[str, Any]:
        return self.call("status")

    def capabilities(self) -> dict[str, Feature]:
        try:
            found = self.call("capabilities").get("features") or {}
        except EngineError as exc:
            return {name: Feature(name, False, exc.message) for name in FEATURES}
        return {
            name: Feature(
                name, bool((found.get(name) or {}).get("available")), str((found.get(name) or {}).get("detail") or "")
            )
            for name in FEATURES
        }

    # ------------------------------------------------------------ observation
    def screen(self) -> Screen:
        data = self.call("screen")
        return Screen(
            int(data["width"]),
            int(data["height"]),
            int(data.get("x") or 0),
            int(data.get("y") or 0),
            float(data.get("scale") or 1.0),
        )

    def screenshot_path(self) -> Path:
        """A new file in HighhX's screenshots folder — the only place an engine may write one."""
        from highhx.utils.paths import user_data_dir

        folder = user_data_dir() / "screenshots"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.session.id}-{next(_SHOTS)}.png"

    def screenshot(self, window: int | None = None, *, path: Path | None = None) -> Screenshot:
        target = path or self.screenshot_path()
        data = self.call("screenshot", path=str(target), window=window)
        return Screenshot(
            Path(data["path"]), int(data["width"]), int(data["height"]), float(data.get("scale") or 1.0), window
        )

    def apps(self) -> list[App]:
        return [
            App(
                str(a.get("name") or ""),
                int(a.get("pid") or 0),
                str(a.get("bundle_id") or ""),
                bool(a.get("frontmost")),
            )
            for a in self.call("apps").get("apps") or []
        ]

    def windows(self, app: str | None = None) -> list[Window]:
        return [Window.from_dict(w) for w in self.call("windows", app=app).get("windows") or []]

    def active(self) -> tuple[str, str, Window | None]:
        """The frontmost application, its window title and (when the platform reports it) its window."""
        data = self.call("frontmost")
        window = Window.from_dict(data["window"]) if isinstance(data.get("window"), dict) else None
        return str(data.get("app") or ""), str(data.get("title") or ""), window

    def get_ui_tree(self, app: str | None = None, *, limit: int = 300) -> Observation:
        """The accessibility tree of ``app`` (default: the frontmost application) as an observation."""
        data = self.call("inspect", app=app, limit=limit)
        elements = [
            UIElement(
                id=f"a{n}",
                role=str(item.get("role") or ""),
                name=str(item.get("name") or ""),
                value="" if item.get("secure") else str(item.get("value") or ""),
                enabled=bool(item.get("enabled", True)),
                focused=bool(item.get("focused")),
                attributes={"type": "password"} if item.get("secure") else {},
                bounds=parse_bounds(item.get("bounds")),
                source="ax",
            )
            for n, item in enumerate(data.get("elements") or [], start=1)
        ]
        return Observation(
            provider="accessibility",
            application=str(data.get("app") or app or ""),
            title=str(data.get("title") or ""),
            elements=elements,
            captured_at=time.time(),
        )

    def element_at(self, x: int, y: int) -> UIElement:
        data = self.call("element_at", x=x, y=y)
        element = UIElement(
            id="at",
            role=str(data.get("role") or ""),
            name=str(data.get("name") or ""),
            value="" if data.get("secure") else str(data.get("value") or ""),
            attributes={"type": "password"} if data.get("secure") else {},
            bounds=parse_bounds(data.get("bounds")),
            source="ax",
        )
        element.attributes["app"] = str(data.get("app") or "")
        return element

    def observe(self, app: str | None = None, *, tree: bool = True, screenshot: bool = False) -> DesktopState:
        """The desktop now: structure (windows, the accessibility tree) and, only when asked, pixels.
        A part the platform cannot provide is left out, never faked."""
        front, title, window = self.active()
        state = DesktopState(front, title, window, self.windows())
        if tree:
            try:
                state.tree = self.get_ui_tree(app)
            except EngineError as exc:
                if exc.code not in ("accessibility_denied", "unsupported_platform", "not_found"):
                    raise
        if screenshot:
            state.screenshot = self.screenshot()
        return state

    def cursor(self) -> tuple[int, int]:
        data = self.call("cursor")
        return int(data["x"]), int(data["y"])

    def running(self, app: str) -> bool:
        return bool(self.call("running", app=app).get("running"))

    def verify(
        self, check: str, *, app: str | None = None, name: str | None = None, role: str | None = None
    ) -> dict[str, Any]:
        return self.call("verify", check=check, app=app, name=name, role=role)

    def verify_state(
        self, window: int, expect: list[dict[str, Any]], *, timeout_ms: int = 5000, stable_samples: int = 2
    ) -> StateCheck:
        """Bounded predicates about one exact window, sampled from fresh state until they hold
        stably (see :mod:`highhx.computer.verify`); ``unknown`` never counts as success."""
        from highhx.computer.verify import verify_state

        return verify_state(self, window, expect, timeout_ms=timeout_ms, stable_samples=stable_samples)

    def clipboard_read(self) -> str:
        return str(self.call("clipboard_read").get("text") or "")

    # ----------------------------------------------------------------- input
    def click(self, x: int, y: int, *, button: str = "left", count: int = 1, app: str | None = None) -> dict[str, Any]:
        """Click at a desktop point; ``app`` delivers it to that application in the background."""
        return self.call("click_at", x=x, y=y, button=button, count=count, app=app)

    def double_click(self, x: int, y: int, *, app: str | None = None) -> dict[str, Any]:
        return self.click(x, y, count=2, app=app)

    def right_click(self, x: int, y: int, *, app: str | None = None) -> dict[str, Any]:
        return self.click(x, y, button="right", app=app)

    def press_element(
        self,
        name: str,
        *,
        role: str | None = None,
        app: str | None = None,
        index: int | None = None,
        bounds: tuple[int, int, int, int] | None = None,
    ) -> dict[str, Any]:
        """Press the element with this accessible name (semantic targeting — no coordinates).

        ``index``: which of several elements with exactly this name (tree order), and ``bounds``:
        where it was observed. With them the engine presses that element or refuses with
        ``stale_target`` when it is gone or has moved; several same-named elements and no
        ``index`` are ``ambiguous_target`` — never a guess."""
        return self.call("click", name=name, role=role, app=app, index=index, bounds=list(bounds) if bounds else None)

    def move(self, x: int, y: int) -> dict[str, Any]:
        return self.call("move", x=x, y=y)

    def drag(
        self, start: tuple[int, int], end: tuple[int, int], *, button: str = "left", duration_ms: int = 300
    ) -> dict[str, Any]:
        return self.call(
            "drag", from_x=start[0], from_y=start[1], to_x=end[0], to_y=end[1], button=button, duration_ms=duration_ms
        )

    def scroll(self, direction: str = "down", amount: int = 3, *, at: tuple[int, int] | None = None) -> dict[str, Any]:
        x, y = at if at is not None else (None, None)
        return self.call("scroll", direction=direction, amount=amount, x=x, y=y)

    def type_text(self, text: str, *, app: str | None = None) -> dict[str, Any]:
        return self.call("type", text=text, app=app)

    def press(self, key: str, *, app: str | None = None) -> dict[str, Any]:
        return self.call("key", key=key, app=app)

    def hotkey(self, keys: str | list[str], *, app: str | None = None) -> dict[str, Any]:
        modifiers, key = parse_hotkey(keys)
        if not modifiers:
            return self.press(key, app=app)
        return self.call("hotkey", modifiers=modifiers, key=key, app=app)

    def clipboard_write(self, text: str) -> dict[str, Any]:
        return self.call("clipboard_write", text=text)

    # ------------------------------------------------ applications and windows
    def launch(self, app: str) -> dict[str, Any]:
        return self.call("launch", app=app)

    def focus(self, app: str) -> dict[str, Any]:
        return self.call("focus", app=app)

    def quit(self, app: str) -> dict[str, Any]:
        return self.call("quit", app=app)

    def open_url(self, url: str, *, app: str | None = None) -> dict[str, Any]:
        return self.call("open_url", url=url, app=app)

    def set_window_frame(self, window: int, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        return self.call("window_frame", window=window, x=x, y=y, width=width, height=height)

    def invoke_menu(self, app: str, path: str | list[str]) -> dict[str, Any]:
        return self.call("menu", app=app, path=menu_path(path))

    def wait(self, ms: int) -> dict[str, Any]:
        return self.call("wait", ms=ms)

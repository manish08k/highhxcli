"""Test doubles for the automation layer: a recording engine behind the real bridge, and a
recording browser flow step — so tests exercise validation, guards, planning, execution and
verification without touching the desktop or a browser."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.handlers import computer
from highhx.actions.spec import ActionResult
from highhx.automation.engine.bridge import AutomationBridge, EngineError
from highhx.automation.engine.protocol import FEATURES
from highhx.computer.driver import HighhXDriver

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)


class FakeEngine:
    """Speaks the bridge protocol from memory: running apps, the frontmost app, sent input."""

    name = "fake"

    def __init__(self) -> None:
        self.front = "Notes"
        self.running: set[str] = {"Notes", "Finder"}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.deny: str | None = None
        """An op that fails with accessibility_denied (to test the error path)."""
        self.focus_fails = False
        self.title = "Untitled"
        self.clicks_change_ui = True
        # version 2: a small desktop
        self.pointer = (0, 0)
        self.clipboard = ""
        self.min_width = 0
        """Windows refuse to be narrower than this (to test an unverified frame)."""
        self.refuse_quit: set[str] = set()
        self.menus = {"File": ["Save", "Close"], "Edit": ["Copy", "Paste"]}
        self.chosen: list[list[str]] = []
        self.windows = [
            {"id": 7, "pid": 70, "app": "Notes", "title": "Untitled", "x": 0, "y": 25, "width": 800, "height": 600}
        ]
        self.elements = [
            {"role": "button", "name": "Save", "bounds": [100, 100, 80, 30]},
            {"role": "button", "name": "Delete", "bounds": [200, 100, 80, 30]},
        ]

    def call(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, args))
        if op == self.deny:
            from highhx.automation.engine.bridge import accessibility_denied

            raise accessibility_denied()
        if op == "status":
            return {"engine": self.name, "protocol": 1, "ok": True, "accessibility": True, "detail": "fake"}
        if op == "frontmost":
            return {"app": self.front, "title": ""}
        if op == "running":
            return {"app": args["app"], "running": args["app"] in self.running}
        if op == "launch":
            self.running.add(args["app"])
            return {"app": args["app"], "running": True}
        if op == "focus":
            if args["app"] not in self.running:
                raise EngineError("not_found", f"{args['app']} is not running")
            if not self.focus_fails:
                self.front = args["app"]
            return {"app": args["app"], "frontmost": self.front == args["app"], "actual": self.front}
        if op == "click" and self.clicks_change_ui:
            self.title = f"after {args['name']}"
        if op == "inspect":
            return {"app": self.front, "title": self.title, "elements": [dict(e) for e in self.elements]}
        return self.version_2(op, args)

    def _at(self, x: int, y: int) -> dict[str, Any] | None:
        for element in self.elements:
            ex, ey, width, height = element["bounds"]
            if ex <= x < ex + width and ey <= y < ey + height:
                return {**element, "app": self.front, "pid": 70}
        return None

    def version_2(self, op: str, args: dict[str, Any]) -> dict[str, Any]:
        if op == "capabilities":
            return {"platform": "fake", "features": {f: {"available": True, "detail": "fake"} for f in FEATURES}}
        if op == "screen":
            return {"width": 1440, "height": 900, "x": 0, "y": 0, "scale": 2.0}
        if op == "screenshot":
            Path(args["path"]).write_bytes(PNG_1X1)
            return {"path": args["path"], "width": 1, "height": 1, "scale": 2.0, "window": args.get("window")}
        if op == "apps":
            return {
                "apps": [
                    {"name": a, "pid": 70, "bundle_id": "", "frontmost": a == self.front} for a in sorted(self.running)
                ]
            }
        if op == "windows":
            return {"windows": [w for w in self.windows if not args.get("app") or w["app"] == args["app"]]}
        if op == "window_frame":
            window = next((w for w in self.windows if w["id"] == args["window"]), None)
            if window is None:
                raise EngineError("not_found", "no such window")
            window.update(x=args["x"], y=args["y"], width=max(args["width"], self.min_width), height=args["height"])
            return {
                "window": window["id"],
                "app": window["app"],
                "frame": [window[k] for k in ("x", "y", "width", "height")],
            }
        if op == "quit":
            if args["app"] not in self.running:
                raise EngineError("not_found", f"{args['app']} is not running")
            if args["app"] in self.refuse_quit:
                return {
                    "app": args["app"],
                    "quit": False,
                    "detail": f"{args['app']} is still running (it may be asking to save)",
                }
            self.running.discard(args["app"])
            return {"app": args["app"], "quit": True, "detail": ""}
        if op == "cursor":
            return {"x": self.pointer[0], "y": self.pointer[1]}
        if op in ("move", "click_at"):
            self.pointer = (args["x"], args["y"])
            if op == "click_at":
                return {**args, "element": self._at(args["x"], args["y"])}
            return {"x": args["x"], "y": args["y"]}
        if op == "drag":
            self.pointer = (args["to_x"], args["to_y"])
            return {"from": [args["from_x"], args["from_y"]], "to": [args["to_x"], args["to_y"]]}
        if op == "element_at":
            found = self._at(args["x"], args["y"])
            if found is None:
                raise EngineError("not_found", "no element there")
            return found
        if op == "menu":
            items = self.menus.get(args["path"][0], [])
            if len(args["path"]) != 2 or args["path"][1] not in items:
                raise EngineError("not_found", f"{args['app']} has no menu item {args['path'][-1]!r}")
            self.chosen.append(list(args["path"]))
            return {"app": args["app"], "path": args["path"]}
        if op == "clipboard_read":
            return {"text": self.clipboard, "has_text": bool(self.clipboard)}
        if op == "clipboard_write":
            self.clipboard = args["text"]
            return {"characters": len(args["text"])}
        return {"op": op, **args}

    def sent(self, *ops: str) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] in ops]

    def close(self) -> None:
        return None


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine()
    driver = HighhXDriver(AutomationBridge(fake))  # the real driver and bridge over a recording engine
    monkeypatch.setattr(ActionExecutor, "driver", lambda self, cancel=None: driver)
    return fake


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The HighhX browser's flow step, recorded: the page 'opens' at the requested URL
    (or at ``redirect[url]``, e.g. a sign-in page)."""
    record: dict[str, Any] = {"flows": [], "argv": [], "redirect": {}}

    def flow(_ctx: Any, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
        record["flows"].append(step)
        url = step.get("open", "")
        landed = record["redirect"].get(url, url)
        return ActionResult(
            True, output={"step": step, "url": landed, "title": "page"}, summary=str(step), verified=True
        )

    def run(_ctx: Any, argv: list[str], **_kw: Any) -> str:
        record["argv"].append(argv)
        return ""

    monkeypatch.setattr(computer, "_flow_step", flow)
    monkeypatch.setattr(computer, "open_url", lambda ctx, url, reuse_tab=False: flow(ctx, {"open": url}))
    monkeypatch.setattr(computer, "_run", run)
    return record


class FakeRuntime:
    """Just enough of the HighhX browser runtime for ``browser.play``: results with one video."""

    def __init__(self) -> None:
        self.observation: Any = None
        self.visited: list[str] = []

    def navigate(self, url: str) -> Any:
        self.visited.append(url)
        self.observation = type("Obs", (), {"url": url, "title": "", "elements": []})()
        return type("Outcome", (), {"ok": True, "problems": []})()

    def observe(self) -> Any:
        link = type("El", (), {"role": "link", "name": "A video", "attributes": {"href": "/watch?v=abc123"}})()
        return type("Obs", (), {"url": self.visited[-1], "elements": [link]})()


@pytest.fixture
def media(monkeypatch: pytest.MonkeyPatch, browser: dict[str, Any]) -> FakeRuntime:
    """browser.play against a fake page whose video starts playing."""
    runtime = FakeRuntime()
    page = type(
        "Page",
        (),
        {"evaluate": lambda self, js, cancel=None, **_kw: {"found": True, "paused": False, "title": "A video"}},
    )()
    session = type("Session", (), {"browser": page, "cancel": None, "_desktop_app": None, "_runtimes": {}})()
    monkeypatch.setattr(computer, "_runtime", lambda _ctx: runtime)
    monkeypatch.setattr(ActionExecutor, "computer", lambda self: session)
    return runtime

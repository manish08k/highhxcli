"""Test doubles for the automation layer: a recording engine behind the real bridge, and a
recording browser flow step — so tests exercise validation, guards, planning, execution and
verification without touching the desktop or a browser."""

from __future__ import annotations

from typing import Any

import pytest

from highhx.actions.executor import ActionExecutor
from highhx.actions.handlers import computer
from highhx.actions.spec import ActionResult
from highhx.automation.engine.bridge import AutomationBridge, EngineError


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
            return {
                "app": self.front,
                "title": self.title,
                "elements": [{"role": "button", "name": "Save"}, {"role": "button", "name": "Delete"}],
            }
        return {"op": op, **args}

    def sent(self, *ops: str) -> list[tuple[str, dict[str, Any]]]:
        return [c for c in self.calls if c[0] in ops]

    def close(self) -> None:
        return None


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine()
    bridge = AutomationBridge(fake)
    monkeypatch.setattr(ActionExecutor, "automation", lambda self, cancel=None: bridge)
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
        "Page", (), {"evaluate": lambda self, js, cancel=None: {"found": True, "paused": False, "title": "A video"}}
    )()
    session = type("Session", (), {"browser": page, "cancel": None, "_desktop_app": None, "_runtimes": {}})()
    monkeypatch.setattr(computer, "_runtime", lambda _ctx: runtime)
    monkeypatch.setattr(ActionExecutor, "computer", lambda self: session)
    return runtime

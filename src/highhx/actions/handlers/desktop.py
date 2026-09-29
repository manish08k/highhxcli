"""computer.* desktop actions — the action executor's side of the HighhX Computer API.

Each handler runs after the executor has validated, classified and approved the action, calls
:class:`~highhx.computer.driver.HighhXDriver` (never an engine or the OS directly) and verifies
what it can observe: the window that should be in front, the pointer where it was moved, a
window's new frame, an application that quit, the clipboard's new text. What cannot be observed
is reported as unverified, never as verified. Input never reaches a terminal (the bridge's
guard); element presses go through the computer runtime, whose per-element safety check asks
before "Delete", "Send" or "Pay".
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from highhx.actions.handlers.native import _flow_step
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError
from highhx.automation.engine.bridge import EngineError
from highhx.automation.engine.protocol import ProtocolError
from highhx.core.errors import UsageError

if TYPE_CHECKING:
    from highhx.computer.driver import HighhXDriver

T = TypeVar("T")
EXPLAINED = frozenset({"refused", "unsupported", "unsupported_platform", "not_found", "screen_recording_denied"})
"""Engine errors that are the action's answer (said to the person), not a crash."""
FRAME_TOLERANCE = 4
"""Points a window may differ from the requested frame (window managers snap and enforce minimums)."""


def driver(ctx: ActionContext) -> HighhXDriver:
    return ctx.executor.driver(ctx.cancel)


def run(ctx: ActionContext, operate: Callable[[HighhXDriver], T]) -> T:
    """One driver call; a refusal or an unavailable feature is the action's error, not a crash."""
    try:
        return operate(driver(ctx))
    except (ProtocolError, UsageError) as exc:
        raise ToolError(str(getattr(exc, "message", exc))) from None
    except EngineError as exc:
        if exc.code in EXPLAINED:
            raise ToolError(exc.message + (f" {exc.hint}" if exc.hint else "")) from None
        raise


def frontmost_app(ctx: ActionContext) -> str:
    return run(ctx, lambda d: d.active()[0])


def _app_name(name: str) -> str:
    from highhx.actions.handlers.computer import _app_for

    app = _app_for(name)
    return str(app.platform_name) if app is not None else name


# ----------------------------------------------------------- applications
def focus(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Bring an application to the front — launching it first when it is not running — and
    verify that it is frontmost."""
    from highhx.actions.handlers.computer import _app_for

    app = _app_for(str(inputs["app"]))
    if app is None:
        raise ToolError("no application given")
    name = app.platform_name
    launched = False
    if not run(ctx, lambda d: d.running(name)):
        launched = bool(run(ctx, lambda d: d.launch(name)).get("running"))
        if not launched:
            return ActionResult(False, output={"app": app.name}, error=f"{app.name} did not start", verified=False)
    focused = run(ctx, lambda d: d.focus(name))
    ok = bool(focused.get("frontmost"))
    session = ctx.computer()
    session._desktop_app = name
    session._runtimes.pop("desktop", None)
    actual = str(focused.get("actual") or "")
    return ActionResult(
        ok,
        output={"app": app.name, "frontmost": ok, "launched": launched, "engine": driver(ctx).name},
        summary=f"{'opened and ' if launched else ''}switched to {app.name}",
        verified=ok,
        error="" if ok else f"{app.name} is not in front ({actual or 'another application'} is)",
    )


def quit_app(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    name = _app_name(str(inputs["app"]))
    result = run(ctx, lambda d: d.quit(name))
    gone = bool(result.get("quit"))
    return ActionResult(
        gone,
        output={"app": name, "quit": gone},
        summary=f"quit {name}" if gone else str(result.get("detail") or f"{name} did not quit"),
        verified=gone,
        error="" if gone else str(result.get("detail") or ""),
    )


def menu(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.computer.driver import menu_path

    name = _app_name(str(inputs["app"]))
    path = menu_path(inputs["path"])
    run(ctx, lambda d: d.invoke_menu(name, path))
    return ActionResult(
        True,
        output={"app": name, "path": path},
        summary=f"chose {' > '.join(path)} in {name}",
        verified=None,  # what a menu item does is up to the application
    )


# --------------------------------------------------------------- keyboard
def _target(inputs: Inputs) -> str | None:
    return _app_name(str(inputs["app"])) if inputs.get("app") else None


def type_text(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    text = str(inputs["text"])
    target = _target(inputs)
    where = target or frontmost_app(ctx)
    run(ctx, lambda d: d.type_text(text, app=target))
    return ActionResult(
        True,
        output={"app": where, "characters": len(text), "background": bool(target)},
        summary=f"typed {len(text)} character(s) into {where}",
    )


def press(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    key = str(inputs["key"]).lower()
    target = _target(inputs)
    where = target or frontmost_app(ctx)
    run(ctx, lambda d: d.press(key, app=target))
    return ActionResult(True, output={"app": where, "key": key}, summary=f"pressed {key} in {where}")


def hotkey(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.computer.driver import parse_hotkey

    try:
        modifiers, key = parse_hotkey(str(inputs["keys"]))
    except UsageError as exc:
        raise ToolError(exc.message) from None
    target = _target(inputs)
    where = target or frontmost_app(ctx)
    run(ctx, lambda d: d.hotkey([*modifiers, key], app=target))
    combo = "+".join([*modifiers, key])
    return ActionResult(True, output={"app": where, "keys": combo}, summary=f"pressed {combo} in {where}")


# ---------------------------------------------------------------- pointer
def click(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """A control by role and name, through the computer runtime (per-element safety, verification)."""
    return desktop_step(ctx, {"click": str(inputs["target"])}, float(inputs.get("timeout") or 10))


def click_at(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """A click at a desktop point — or at the text named by ``text``, grounded by perception
    (accessibility first, OCR as the fallback)."""
    grounded: dict[str, Any] | None = None
    if inputs.get("text"):
        from highhx.computer.perception import GroundingError, ground

        try:
            found = run(ctx, lambda d: ground(d, str(inputs["text"]), app=_target(inputs)))
        except GroundingError as exc:
            raise ToolError(exc.message) from None
        x, y = found.point
        grounded = found.to_dict()
    elif inputs.get("x") is not None and inputs.get("y") is not None:
        x, y = int(inputs["x"]), int(inputs["y"])
    else:
        raise ToolError("give x and y, or the text to click")
    button, count = str(inputs.get("button") or "left"), int(inputs.get("count") or 1)
    target = _target(inputs) if inputs.get("background") else None
    result = run(ctx, lambda d: d.click(x, y, button=button, count=count, app=target))
    hit = result.get("element") or {}
    what = f"{hit.get('role')} {hit.get('name')!r}" if hit.get("name") else f"({x}, {y})"
    kind = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}[count]
    return ActionResult(
        True,
        output={"x": x, "y": y, "button": button, "count": count, "element": hit or None, "grounded": grounded},
        summary=f"{button + '-' if button != 'left' else ''}{kind} {what}",
        verified=None,  # the effect of a click at a point is the application's; observe to check it
    )


def move(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    x, y = int(inputs["x"]), int(inputs["y"])
    run(ctx, lambda d: d.move(x, y))
    actual = run(ctx, lambda d: d.cursor())
    ok = abs(actual[0] - x) <= 2 and abs(actual[1] - y) <= 2
    return ActionResult(
        ok,
        output={"x": x, "y": y, "cursor": list(actual)},
        summary=f"moved the pointer to ({x}, {y})",
        verified=ok,
        error="" if ok else f"the pointer is at {actual}, not ({x}, {y})",
    )


def drag(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    start = (int(inputs["from_x"]), int(inputs["from_y"]))
    end = (int(inputs["to_x"]), int(inputs["to_y"]))
    button = str(inputs.get("button") or "left")
    run(ctx, lambda d: d.drag(start, end, button=button, duration_ms=int(inputs.get("duration_ms") or 300)))
    return ActionResult(
        True, output={"from": list(start), "to": list(end)}, summary=f"dragged from {start} to {end}", verified=None
    )


def scroll(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    direction = str(inputs.get("direction") or "down")
    if str(inputs.get("source") or "browser") == "browser":
        return _flow_step(ctx, {"scroll": direction})
    at = (int(inputs["x"]), int(inputs["y"])) if inputs.get("x") is not None and inputs.get("y") is not None else None
    where = frontmost_app(ctx)
    run(ctx, lambda d: d.scroll(direction, int(inputs.get("amount") or 3), at=at))
    return ActionResult(True, output={"app": where, "direction": direction}, summary=f"scrolled {direction} in {where}")


# ------------------------------------------------------------- observation
def observe(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    app = _target(inputs)
    state = run(
        ctx, lambda d: d.observe(app, tree=bool(inputs.get("tree", True)), screenshot=bool(inputs.get("screenshot")))
    )
    count = len(state.tree.elements) if state.tree else 0
    parts = [f"{state.app or 'no application'} in front", f"{len(state.windows)} window(s)"]
    parts.append(f"{count} element(s)" if state.tree else "no accessibility tree")
    if state.screenshot:
        parts.append(f"screenshot {state.screenshot.path}")
    return ActionResult(True, output=state.to_dict(), summary=" · ".join(parts))


def screenshot(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    window = int(inputs["window"]) if inputs.get("window") is not None else None
    shot = run(ctx, lambda d: d.screenshot(window))
    ok = shot.path.is_file() and shot.width > 0
    return ActionResult(
        ok,
        output={"path": str(shot.path), "width": shot.width, "height": shot.height, "scale": shot.scale},
        summary=f"{shot.width}x{shot.height} screenshot: {shot.path}",
        verified=ok,
    )


def windows(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    app = _target(inputs)
    found = run(ctx, lambda d: d.windows(app))
    front, title, _window = run(ctx, lambda d: d.active())
    return ActionResult(
        True,
        output={"frontmost": front, "title": title, "windows": [w.__dict__ for w in found]},
        summary=f"{len(found)} window(s); {front or 'nothing'} in front",
    )


def apps(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    found = run(ctx, lambda d: d.apps())
    front = next((a.name for a in found if a.frontmost), "")
    return ActionResult(
        True,
        output={"apps": [a.__dict__ for a in found]},
        summary=f"{len(found)} application(s); {front or 'none'} in front",
    )


def element_at(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    x, y = int(inputs["x"]), int(inputs["y"])
    element = run(ctx, lambda d: d.element_at(x, y))
    return ActionResult(
        True,
        output={**element.to_dict(), "bounds": list(element.bounds) if element.bounds else None},
        summary=f"{element.label()} at ({x}, {y}) in {element.attributes.get('app') or 'an application'}",
    )


def window_frame(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    wanted = [int(inputs[k]) for k in ("x", "y", "width", "height")]
    if inputs.get("window") is not None:
        window_id = int(inputs["window"])
    elif inputs.get("app"):
        name = _app_name(str(inputs["app"]))
        found = run(ctx, lambda d: d.windows(name))
        if not found:
            raise ToolError(f"{name} has no window on screen.")
        window_id = found[0].id
    else:
        raise ToolError("give the window id or the application")
    result = run(ctx, lambda d: d.set_window_frame(window_id, *wanted))
    frame = [int(v) for v in result.get("frame") or []]
    ok = len(frame) == 4 and all(abs(a - b) <= FRAME_TOLERANCE for a, b in zip(frame, wanted, strict=True))
    return ActionResult(
        ok,
        output={"window": window_id, "app": result.get("app"), "frame": frame},
        summary=f"moved {result.get('app') or 'the window'} to {wanted}",
        verified=ok,
        error="" if ok else f"the window is at {frame} (the application limits its size or position)",
    )


def clipboard_read(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    text = run(ctx, lambda d: d.clipboard_read())
    return ActionResult(
        True, output={"text": text, "characters": len(text)}, summary=f"{len(text)} character(s) on the clipboard"
    )


def clipboard_write(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    text = str(inputs["text"])
    run(ctx, lambda d: d.clipboard_write(text))
    ok = run(ctx, lambda d: d.clipboard_read()) == text
    return ActionResult(
        ok,
        output={"characters": len(text)},
        summary=f"put {len(text)} character(s) on the clipboard",
        verified=ok,
        error="" if ok else "the clipboard holds something else after writing",
    )


# ----------------------------------------------------------- the runtime
def desktop_step(ctx: ActionContext, step: dict[str, Any], timeout: float = 10.0) -> ActionResult:
    """One step through the computer runtime over the driver: element resolution, the per-element
    safety check, the action, and re-observation to verify it."""
    from highhx.automation.engine.provider import BridgeDesktopProvider
    from highhx.computer.flows import Flow, FlowRunner
    from highhx.computer.runtime import ComputerRuntime

    session = ctx.computer()
    provider = BridgeDesktopProvider(driver(ctx), session._desktop_app)
    runtime = ComputerRuntime(provider, session.gate, actor=session.actor, tool=session.tool, cancel=ctx.cancel)
    result = FlowRunner(runtime, launch=session.launch).run(Flow("action", [step], timeout))
    entry: dict[str, Any] = result.steps[0] if result.steps else {"ok": False, "error": "nothing ran"}
    detail = str(entry.get("error") or "; ".join(str(p) for p in entry.get("problems") or []))
    verified = entry.get("verified")
    return ActionResult(
        bool(result.ok),
        output={"step": entry},
        summary=str(entry.get("action", "")),
        verified=verified if isinstance(verified, bool) else None,
        error="" if result.ok else detail,
    )

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
EXPLAINED = frozenset(
    {
        "refused",
        "unsupported",
        "unsupported_platform",
        "not_found",
        "screen_recording_denied",
        "stale_target",
        "ambiguous_target",
    }
)
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
        if exc.code == "connection_lost":
            from highhx.automation.engine.remote import LOST

            raise ToolError(f"{LOST}: {exc.message} Whether this action happened is unknown; HighhX reconnects for the next one.") from None
        if exc.code in EXPLAINED:
            raise ToolError(exc.message + (f" {exc.hint}" if exc.hint else "")) from None
        raise


def _point(ctx: ActionContext, inputs: Inputs, x_key: str = "x", y_key: str = "y") -> tuple[int, int]:
    """A desktop point from ``inputs``: desktop points as given — or, with ``capture``, a position
    in that screenshot (``space``: pixels or relative1000), grounded and checked by the session's
    capture store; a stale screenshot is refused, never clicked."""
    from highhx.computer.capture import StaleCapture

    if inputs.get(x_key) is None or inputs.get(y_key) is None:
        raise ToolError(f"give {x_key} and {y_key}")
    x, y = inputs[x_key], inputs[y_key]
    if not inputs.get("capture"):
        return int(x), int(y)
    store = ctx.computer().captures
    try:
        return run(
            ctx,
            lambda d: store.ground(d, str(inputs["capture"]), float(x), float(y), space=str(inputs.get("space") or "pixels")),
        )
    except StaleCapture as exc:
        raise ToolError(f"{STALE}: {exc.message}") from None


STALE = "Stale screenshot"
"""How a refused, out-of-date screenshot coordinate starts its error (callers take a new one)."""


def frontmost_app(ctx: ActionContext) -> str:
    return run(ctx, lambda d: d.active()[0])


def _app_name(name: str) -> str:
    from highhx.actions.handlers.computer import _app_for

    app = _app_for(name)
    return str(app.platform_name) if app is not None else name


# ----------------------------------------------------------- applications
def focus(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Bring an application to the front — launching it first when it is not running — or one
    exact window (by id), and verify that it is frontmost."""
    from highhx.actions.handlers.computer import _app_for

    if inputs.get("window") is not None:
        return _focus_window(ctx, int(inputs["window"]))
    if not inputs.get("app"):
        raise ToolError("give the application or the window id")
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


def _focus_window(ctx: ActionContext, window: int) -> ActionResult:
    result = run(ctx, lambda d: d.focus_window(window))
    front = run(ctx, lambda d: d.windows())
    ok = bool(result.get("frontmost")) and bool(front) and front[0].id == window
    where = str(result.get("app") or f"window {window}")
    actual = f"{front[0].app} window {front[0].id}" if front else "no window"
    return ActionResult(
        ok,
        output={"window": window, "app": result.get("app"), "frontmost": ok},
        summary=f"brought {where} (window {window}) to the front",
        verified=ok,
        error="" if ok else f"window {window} is not in front ({actual} is)",
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


READBACK_SECONDS = 1.5
"""How long typed text may take to show in the focused field before it counts as missing."""


def type_text(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Type, then read the focused field back: the text there is verified; a readable field without
    it is a failure (the keys went elsewhere); a secret or unreadable field stays unverified."""
    text = str(inputs["text"])
    target = _target(inputs)
    where = target or frontmost_app(ctx)
    run(ctx, lambda d: d.type_text(text, app=target))
    verified, field_name = _typed_readback(ctx, where, text)
    detail = f" ({field_name})" if field_name else ""
    return ActionResult(
        verified is not False,
        output={"app": where, "characters": len(text), "background": bool(target), "field": field_name or None},
        summary=f"typed {len(text)} character(s) into {where}{detail}",
        verified=verified,
        error="" if verified is not False else f"the focused field{detail} does not show the typed text",
    )


def _typed_readback(ctx: ActionContext, app: str, text: str) -> tuple[bool | None, str]:
    import time

    wanted = " ".join(text.split())
    deadline = time.monotonic() + READBACK_SECONDS
    while True:
        try:
            tree = ctx.executor.driver(ctx.cancel).get_ui_tree(app)
        except EngineError:
            return None, ""  # the tree is not readable here: unverified, never assumed
        focused = next((e for e in tree.elements if e.focused and e.role in ("textbox", "searchbox")), None)
        if focused is None:
            return None, ""
        label = focused.name or focused.role
        if focused.secret:
            return None, label
        if wanted and wanted in " ".join(focused.value.split()):
            return True, label
        if time.monotonic() >= deadline or len(focused.value) >= 300:  # values are read up to 300 characters
            return (False if len(focused.value) < 300 else None), label
        if ctx.cancel.wait(0.25):
            return None, label


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
    (accessibility first, OCR as the fallback) and clicked only while the window it was found in
    is still there, unmoved and uncovered."""
    grounded: dict[str, Any] | None = None
    found = None
    if inputs.get("text"):
        from highhx.computer.perception import GroundingError, ground

        try:
            found = run(ctx, lambda d: ground(d, str(inputs["text"]), app=_target(inputs)))
        except GroundingError as exc:
            raise ToolError(exc.message) from None
        x, y = found.point
        grounded = found.to_dict()
    elif inputs.get("x") is not None and inputs.get("y") is not None:
        x, y = _point(ctx, inputs)
    else:
        raise ToolError("give x and y, or the text to click")
    button, count = str(inputs.get("button") or "left"), int(inputs.get("count") or 1)
    target = _target(inputs) if inputs.get("background") else None
    if found is not None:
        from highhx.computer.perception import still_there

        stale = run(ctx, lambda d: still_there(d, found))
        if stale:
            raise ToolError(f"Not clicking {inputs['text']!r}: {stale}. Find it again.")
    result = run(ctx, lambda d: d.click(x, y, button=button, count=count, app=target))
    hit = result.get("element") or {}
    what = f"{hit.get('role')} {hit.get('name')!r}" if hit.get("name") else f"({x}, {y})"
    kind = {1: "clicked", 2: "double-clicked", 3: "triple-clicked"}[count]
    return ActionResult(
        True,
        output={
            "x": x,
            "y": y,
            "button": button,
            "count": count,
            "element": hit or None,
            "grounded": grounded,
            "capture": inputs.get("capture"),
        },
        summary=f"{button + '-' if button != 'left' else ''}{kind} {what}",
        verified=None,  # the effect of a click at a point is the application's; observe to check it
    )


def move(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    x, y = _point(ctx, inputs)
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


def mouse_button(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Only the press, or only the release, of a button — for drags and long presses that
    ``computer.drag`` cannot express. The pointer's arrival is verified; what the press does is the
    application's."""
    (x, y), action = _point(ctx, inputs), str(inputs["action"])
    button = str(inputs.get("button") or "left")
    run(ctx, lambda d: d.mouse_down(x, y, button=button) if action == "down" else d.mouse_up(x, y, button=button))
    actual = run(ctx, lambda d: d.cursor())
    arrived = abs(actual[0] - x) <= 2 and abs(actual[1] - y) <= 2
    return ActionResult(
        arrived,
        output={"action": action, "x": x, "y": y, "button": button, "cursor": list(actual)},
        summary=f"{'pressed' if action == 'down' else 'released'} the {button} button at ({x}, {y})",
        verified=None if arrived else False,
        error="" if arrived else f"the pointer is at {actual}, not ({x}, {y})",
    )


def drag(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    start = _point(ctx, inputs, "from_x", "from_y")
    end = _point(ctx, inputs, "to_x", "to_y")
    button = str(inputs.get("button") or "left")
    run(ctx, lambda d: d.drag(start, end, button=button, duration_ms=int(inputs.get("duration_ms") or 300)))
    return ActionResult(
        True, output={"from": list(start), "to": list(end)}, summary=f"dragged from {start} to {end}", verified=None
    )


def scroll(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    direction = str(inputs.get("direction") or "down")
    if str(inputs.get("source") or "browser") == "browser":
        return _flow_step(ctx, {"scroll": direction})
    at = _point(ctx, inputs) if inputs.get("x") is not None and inputs.get("y") is not None else None
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


def _front_window(ctx: ActionContext, app: str) -> int:
    """The id of ``app``'s front window (windows are listed front to back)."""
    name = _app_name(app)
    found = run(ctx, lambda d: d.windows(name))
    if not found:
        raise ToolError(f"{name} has no window on screen.")
    return found[0].id


def screenshot(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    window = int(inputs["window"]) if inputs.get("window") is not None else None
    if window is None and inputs.get("app"):
        window = _front_window(ctx, str(inputs["app"]))
    region: tuple[int, int, int, int] | None = None
    if inputs.get("region"):
        x, y, width, height = (int(v) for v in inputs["region"])
        region = (x, y, width, height)
    if region is not None and window is not None:
        raise ToolError("give a window (or app) or a region, not both")
    max_size = int(inputs["max_size"]) if inputs.get("max_size") else None
    store = ctx.computer().captures
    capture = run(ctx, lambda d: store.take(d, window=window, region=region, max_size=max_size))
    shot = capture.shot
    ok = shot.path.is_file() and shot.width > 0
    return ActionResult(
        ok,
        output={**capture.to_dict(), "window": window, "region": list(region) if region else None},
        summary=f"{shot.width}x{shot.height} {capture.id}: {shot.path}",
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


def verify(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    """Checked observation of one exact window: by id, an application's front window, or (neither
    given) the frontmost application's front window."""
    if inputs.get("window") is not None:
        window_id = int(inputs["window"])
    elif inputs.get("app"):
        window_id = _front_window(ctx, str(inputs["app"]))
    else:
        front_app, _title, active = run(ctx, lambda d: d.active())
        front = active or next(iter(run(ctx, lambda d: d.windows(front_app) if front_app else [])), None)
        if front is None:
            raise ToolError("No window is in front to verify; give the window id or the application.")
        window_id = front.id
    check = run(
        ctx,
        lambda d: d.verify_state(
            window_id,
            list(inputs["expect"]),
            timeout_ms=int(inputs.get("timeout_ms", 5000)),
            stable_samples=int(inputs.get("stable_samples", 2)),
        ),
    )
    failing = [p for p in check.predicates if p.status != "satisfied"]
    return ActionResult(
        check.ok,
        output=check.to_dict(),
        summary=f"{len(check.predicates) - len(failing)}/{len(check.predicates)} predicate(s) hold: {check.status}",
        verified=True if check.ok else (None if check.status == "unknown" else False),
        error="" if check.ok else "; ".join(f"#{p.index} {p.status}: {p.detail}" for p in failing),
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

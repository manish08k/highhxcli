"""``android.*`` handlers: an Android device through adb, after the executor approved the action.

Taps can name their target (``text``). It is then grounded on the device's own UI hierarchy
(accessibility → resource id → text) and the tap goes to the one element found. Several equal
matches are an error, never a guess. Launches are verified by the focused package and typing by
the focused field's value. Taps and swipes report their effect as not verified, and the agent
loop observes again to check it.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.tools.base import ToolError
from highhx.drivers.android.adb import AdbClient, AdbError
from highhx.drivers.android.driver import AndroidDriver

LAUNCH_WAIT = 6.0


def _adb(ctx: ActionContext, serial: str | None = None) -> AdbClient:
    factory = getattr(ctx.computer(), "android_client", None)
    return factory(serial or None, ctx.cancel) if factory is not None else AdbClient(serial=serial or None, cancel=ctx.cancel)


def _driver(ctx: ActionContext, inputs: Inputs) -> AndroidDriver:
    adb = _adb(ctx, inputs.get("device"))
    if not adb.capability().available:
        raise ToolError(adb.capability().detail)
    try:
        adb.require_device()
    except AdbError as exc:
        raise ToolError(exc.message + (f" ({exc.hint})" if exc.hint else "")) from None
    return AndroidDriver(adb)


def _ground(driver: AndroidDriver, text: str, role: str | None) -> tuple[int, int, dict[str, Any]]:
    from highhx.grounding import HybridGrounder, Target

    state = driver.observe()
    result = HybridGrounder().ground(state, Target.of(text, role or ""), strategies=("accessibility", "dom", "text"))
    if result.status == "ambiguous":
        names = ", ".join(f"{c.element.label()} at {list(c.element.bounds or ())}" for c in result.alternatives[:4] if c.element)
        raise ToolError(f"{text!r} matches several elements ({names}); be more specific (role, or coordinates).")
    point = result.point
    if not result.grounded or point is None:
        raise ToolError(f"{text!r} is not on the screen ({'; '.join(a.detail for a in result.attempts if a.detail)})")
    return point[0], point[1], result.to_dict()


def devices(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    adb = _adb(ctx)
    capability = adb.capability()
    if not capability.available:
        return ActionResult(True, output={"available": False, "detail": capability.detail, "devices": []}, summary=capability.detail)
    found = adb.devices()
    return ActionResult(
        True,
        output={"available": True, "adb": capability.detail, "devices": [d.to_dict() for d in found]},
        summary=f"{len(found)} device(s)" + (": " + ", ".join(f"{d.serial} ({d.state})" for d in found) if found else ""),
    )


def connect(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    adb = _adb(ctx)
    out = adb.connect(str(inputs["address"]))
    ready = any(d.serial == inputs["address"] and d.ready for d in adb.devices())
    return ActionResult(ready, output={"message": out}, summary=out, verified=ready, error="" if ready else out)


def screenshot(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.actions.handlers.state import captures_dir

    data = _driver(ctx, inputs).adb.screencap()
    folder = captures_dir().parent / "android"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"android-{time.strftime('%Y%m%d-%H%M%S')}.png"
    path.write_bytes(data)
    from highhx.perception.png import png_size

    width, height = png_size(data)
    return ActionResult(True, output={"path": str(path), "width": width, "height": height}, summary=f"saved {path.name} ({width}x{height})")


def observe(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    from highhx.actions.handlers.state import computer_state

    return computer_state(ctx, {**inputs, "surface": "android"})


def tap(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    grounded = None
    if inputs.get("text"):
        x, y, grounded = _ground(driver, str(inputs["text"]), inputs.get("role"))
    elif inputs.get("x") is not None and inputs.get("y") is not None:
        x, y = int(inputs["x"]), int(inputs["y"])
    else:
        raise ToolError("give x and y, or the text to tap")
    if inputs.get("long"):
        driver.long_press(x, y, int(inputs.get("ms") or 800))
    else:
        driver.click(x, y)
    what = repr(inputs["text"]) if inputs.get("text") else f"({x}, {y})"
    return ActionResult(
        True,
        output={"x": x, "y": y, "grounded": grounded},
        summary=f"{'long-pressed' if inputs.get('long') else 'tapped'} {what}",
        verified=None,
    )


def swipe(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    if inputs.get("direction"):
        driver.scroll(str(inputs["direction"]), int(inputs.get("amount") or 3))
        return ActionResult(True, output={"direction": inputs["direction"]}, summary=f"scrolled {inputs['direction']}", verified=None)
    points = [inputs.get(k) for k in ("x1", "y1", "x2", "y2")]
    if any(p is None for p in points):
        raise ToolError("give x1, y1, x2, y2 — or a direction")
    x1, y1, x2, y2 = (int(p) for p in points)  # type: ignore[arg-type]
    driver.swipe(x1, y1, x2, y2, int(inputs.get("ms") or 300))
    return ActionResult(True, output={"from": [x1, y1], "to": [x2, y2]}, summary=f"swiped ({x1},{y1}) → ({x2},{y2})", verified=None)


def type_text(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    text = str(inputs["text"])
    driver.type(text)
    focused = next((e for e in driver.observe().elements if e.focused and e.role == "textbox"), None)
    if focused is None:
        return ActionResult(True, output={"characters": len(text)}, summary=f"typed {len(text)} character(s)", verified=None)
    if focused.secret:
        return ActionResult(True, output={"characters": len(text)}, summary="typed into a password field", verified=None)
    ok = focused.value.endswith(text)
    return ActionResult(
        ok,
        output={"characters": len(text), "field": focused.name},
        summary=f"typed {len(text)} character(s) into {focused.label()}",
        verified=ok,
        error="" if ok else f"the field shows {focused.value[-60:]!r}",
    )


def key(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    _driver(ctx, inputs).key(str(inputs["key"]))
    return ActionResult(True, output={"key": inputs["key"]}, summary=f"pressed {inputs['key']}", verified=None)


def back(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return key(ctx, {**inputs, "key": "back"})


def home(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    return key(ctx, {**inputs, "key": "home"})


def launch(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    package = str(inputs["package"])
    driver.adb.launch(package, inputs.get("activity"))
    deadline = time.monotonic() + LAUNCH_WAIT
    current = ""
    while time.monotonic() < deadline:
        current, _activity = driver.adb.current_app()
        if current == package or ctx.cancel.wait(0.4):
            break
    ok = current == package
    return ActionResult(
        ok,
        output={"package": package, "focused": current},
        summary=f"launched {package}" if ok else f"{package} did not come to the front ({current or 'unknown'} is)",
        verified=ok,
        error="" if ok else f"{current or 'another app'} is in front",
    )


def stop(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    package = str(inputs["package"])
    driver.adb.stop(package)
    current, _ = driver.adb.current_app()
    ok = current != package
    return ActionResult(ok, output={"package": package, "focused": current}, summary=f"stopped {package}", verified=ok)


def packages(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    found = _driver(ctx, inputs).adb.packages(third_party=bool(inputs.get("third_party")))
    return ActionResult(True, output={"packages": found}, summary=f"{len(found)} package(s)")


def install(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    apk = Path(str(inputs["apk"])).expanduser()
    if not apk.is_absolute():
        apk = ctx.app.root / apk
    out = _driver(ctx, inputs).adb.install(apk)
    ok = "Success" in out
    return ActionResult(ok, output={"message": out}, summary=f"installed {apk.name}" if ok else out, verified=ok, error="" if ok else out)


def uninstall(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    driver = _driver(ctx, inputs)
    package = str(inputs["package"])
    out = driver.adb.uninstall(package)
    ok = "Success" in out and package not in driver.adb.packages()
    return ActionResult(ok, output={"message": out}, summary=f"uninstalled {package}" if ok else out, verified=ok, error="" if ok else out)

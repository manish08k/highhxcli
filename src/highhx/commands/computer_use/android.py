"""highhx android — Android devices through adb. Every command is an android.* action (risk,
approval, audit, verification)."""

from __future__ import annotations

from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.computer.main import run_action
from highhx.commands.groups import DefaultGroup

DEVICE = click.option("--device", "-s", metavar="SERIAL", help="Device serial (default: the only connected device).")


def _device(device: str | None) -> dict[str, Any]:
    return {"device": device} if device else {}


@click.group("android", cls=DefaultGroup, default_command="devices", short_help="Android devices and emulators (adb).")
def android() -> None:
    """Operate an Android phone, tablet or emulator through adb: observe the UI hierarchy, tap by
    text or point, type, swipe, launch apps — each step classified, approved and verified like
    any HighhX action. Without adb, every command says what to install."""


@android.command("devices", short_help="Connected devices and emulators.")
@pass_app
def android_devices(app: App) -> int:
    """List devices (serial, state, model). Tells you how to set up adb when it is missing."""

    def render(output: dict[str, Any]) -> None:
        if not output.get("available"):
            app.output.warn(str(output.get("detail")))
        rows = [(d["serial"], d["state"], d["model"], "emulator" if d["emulator"] else "device") for d in output.get("devices") or []]
        app.output.table(["serial", "state", "model", "kind"], rows)

    return run_action(app, "android.devices", {}, render)


@android.command("connect", short_help="Connect to a device over the network.")
@click.argument("address")
@pass_app
def android_connect(app: App, address: str) -> int:
    """adb connect HOST:PORT (wireless debugging or a networked emulator)."""
    return run_action(app, "android.connect", {"address": address})


@android.command("observe", short_help="The UI hierarchy of the screen.")
@DEVICE
@click.option("--screenshot", is_flag=True, help="Capture a screenshot too.")
@pass_app
def android_observe(app: App, device: str | None, screenshot: bool) -> int:
    """Controls on screen (role, name, bounds, resource id) and the focused app."""

    def render(output: dict[str, Any]) -> None:
        state = output.get("state") or {}
        app.output.heading(f"{state.get('active_app') or 'Android'}")
        app.output.lines([f"[{e['id']}] {e['role']} {e['name']!r} {e.get('bounds')}" for e in state.get("elements") or []], empty="(no controls)")

    return run_action(app, "android.observe", {**_device(device), **({"screenshot": True} if screenshot else {})}, render)


@android.command("screenshot", short_help="Save a screenshot (PNG).")
@DEVICE
@pass_app
def android_screenshot(app: App, device: str | None) -> int:
    """Save the device screen to HighhX's screenshots folder."""
    return run_action(app, "android.screenshot", _device(device))


@android.command("tap", short_help="Tap a control by text, or a point X,Y.")
@click.argument("target")
@click.option("--long", "long_press", is_flag=True, help="Long press.")
@click.option("--role", help="The control's role (button, textbox …) when the text is ambiguous.")
@DEVICE
@pass_app
def android_tap(app: App, target: str, long_press: bool, role: str | None, device: str | None) -> int:
    """TARGET is visible text or a content description ("Save"), or device pixels "540,1200"."""
    inputs: dict[str, Any] = _device(device)
    parts = target.split(",")
    if len(parts) == 2 and all(p.strip().isdigit() for p in parts):
        inputs.update(x=int(parts[0]), y=int(parts[1]))
    else:
        inputs["text"] = target
        if role:
            inputs["role"] = role
    return run_action(app, "android.long_press" if long_press else "android.tap", inputs)


@android.command("type", short_help="Type into the focused field.")
@click.argument("text")
@DEVICE
@pass_app
def android_type(app: App, text: str, device: str | None) -> int:
    """Type TEXT (verified by the field's value; never read back from password fields)."""
    return run_action(app, "android.type", {"text": text, **_device(device)})


@android.command("swipe", short_help="Swipe a direction, or X1,Y1 X2,Y2.")
@click.argument("points", nargs=-1, required=True)
@DEVICE
@pass_app
def android_swipe(app: App, points: tuple[str, ...], device: str | None) -> int:
    """`highhx android swipe up` or `highhx android swipe 540,1500 540,400`."""
    if len(points) == 1:
        return run_action(app, "android.swipe", {"direction": points[0], **_device(device)})
    (x1, y1), (x2, y2) = (tuple(int(v) for v in p.split(",")) for p in points[:2])
    return run_action(app, "android.swipe", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, **_device(device)})


@android.command("press", short_help="Send a key (enter, back, home, tab …).")
@click.argument("key")
@DEVICE
@pass_app
def android_press(app: App, key: str, device: str | None) -> int:
    """A key event: enter, back, home, tab, delete, app_switch, or KEYCODE_…."""
    return run_action(app, "android.key", {"key": key, **_device(device)})


@android.command("back", short_help="Press Back.")
@DEVICE
@pass_app
def android_back(app: App, device: str | None) -> int:
    """Press the Back key."""
    return run_action(app, "android.back", _device(device))


@android.command("home", short_help="Go to the home screen.")
@DEVICE
@pass_app
def android_home(app: App, device: str | None) -> int:
    """Press the Home key."""
    return run_action(app, "android.home", _device(device))


@android.command("recents", short_help="Show recent apps.")
@DEVICE
@pass_app
def android_recents(app: App, device: str | None) -> int:
    """Open the recent-apps switcher."""
    return run_action(app, "android.recents", _device(device))


@android.command("launch", short_help="Launch an app by package.")
@click.argument("package")
@click.option("--activity", help="A specific activity.")
@DEVICE
@pass_app
def android_launch(app: App, package: str, activity: str | None, device: str | None) -> int:
    """Launch PACKAGE (e.g. com.android.settings), verified by what comes to the front."""
    return run_action(app, "android.launch", {"package": package, **({"activity": activity} if activity else {}), **_device(device)})


@android.command("find", short_help="Find a control by text (never acts).")
@click.argument("text")
@DEVICE
@pass_app
def android_find(app: App, text: str, device: str | None) -> int:
    """Where TEXT is on screen; several matches are listed, never guessed."""
    return run_action(app, "android.find", {"text": text, **_device(device)})


@android.command("agent", short_help="Work toward a goal on the device (agent loop).")
@click.argument("goal")
@click.option("--plan", "plan_file", type=click.Path(exists=True, dir_okay=False), help="Steps from a file (no AI).")
@click.option("--model", is_flag=True, help="Plan with a model (HighhX Pro or a local model).")
@click.option("--remote-model", is_flag=True, help="Allow a remote model to see the task and screen.")
@click.option("--max-steps", type=click.IntRange(1, 200), default=30, show_default=True)
@click.option("--live/--no-live", default=True, help="Show the live dashboard.")
@DEVICE
@pass_app
def android_agent(app: App, goal: str, plan_file: str | None, model: bool, remote_model: bool, max_steps: int, live: bool, device: str | None) -> int:
    """The agent loop on Android: observe the hierarchy, ground targets, act through android.*
    actions, verify, recover. Steps come from --plan, a model (--model), or HighhX Free's
    resolver."""
    from highhx.commands.computer_use.agent import run_goal

    return run_goal(app, goal, surface="android", plan_file=plan_file, model=model, remote_model=remote_model, max_steps=max_steps, live=live, device=device or "")

"""highhx computer — deterministic browser and desktop automation (HighhX Free)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.markup import escape

from highhx.commands import App, pass_app
from highhx.core.errors import UsageError

if TYPE_CHECKING:
    from highhx.computer.runtime import ActionOutcome
    from highhx.computer.session import ComputerSession

SOURCES = ("browser", "desktop")
OBSERVE_SOURCES = (*SOURCES, "screen")

SOURCE_OPTION = click.option(
    "--source",
    type=click.Choice(SOURCES),
    default="browser",
    show_default=True,
    help="browser (DevTools DOM) or desktop (native accessibility, macOS).",
)


def computer_session(app: App, *, headless: bool | None = None, agent: bool = False) -> ComputerSession:
    """A session whose actions are the user's own (Actor.USER), gated, confirmed and audited —
    or, with ``agent``, proposed by the AI planner (Actor.AGENT: the stricter agent rules apply)."""
    from highhx.agent.ui import TerminalUI
    from highhx.computer.session import ComputerSession
    from highhx.safety.actions import Actor
    from highhx.safety.audit import AuditLog
    from highhx.safety.gate import ActionGate, ApprovalMode

    ui = TerminalUI(app.output.console, app.output.symbols, interactive=app.options.is_interactive())
    gate = ActionGate(
        app.engine,
        ui,
        source="computer",
        mode=ApprovalMode.ASK,
        assume_yes=app.options.yes,
        audit=AuditLog(app.db, app.redactor) if app.db is not None else None,
    )
    actor = Actor.AGENT if agent else Actor.USER
    return ComputerSession(gate, actor=actor, cancel=app.ctx.cancel, headless=headless)


def render_outcome(app: App, outcome: ActionOutcome) -> int:
    out = app.output
    data = outcome.to_dict()
    tag = " (verified)" if outcome.verified else (" (not verifiable)" if outcome.verified is None else "")

    def render() -> None:
        if outcome.ok:
            out.success(outcome.summary + tag)
        else:
            out.error(outcome.summary)
            for problem in outcome.problems:
                out.note(f"  {problem}")
        if outcome.observation is not None:
            out.note(
                f"  now: {outcome.observation.title or '-'} <{outcome.observation.url or outcome.observation.application}>"
            )

    out.emit(data, render)
    return 0 if outcome.ok else 1


@click.group("computer", short_help="Browser and desktop automation with known targets (no AI).")
@click.option("--headless/--visible", default=None, help="Run the HighhX browser without a window (default: visible).")
@click.pass_context
def computer(ctx: click.Context, headless: bool | None) -> None:
    """Deterministic UI automation: open apps and pages, observe controls by role and
    name, click, type, press keys and run flow files — each step is checked by the
    HighhX safety policy, sensitive steps (submit, pay, delete …) ask for
    confirmation, and every step is verified and recorded in `highhx audit`.

    Targets are selectors: `Search`, `button:Search`, `textbox="Email"`, `link:Docs#2`.
    """
    ctx.meta["highhx_headless"] = headless


def _headless() -> bool | None:
    return click.get_current_context().meta.get("highhx_headless")


@computer.command("status", short_help="Which automation capabilities are available here.")
@pass_app
def computer_status(app: App) -> int:
    """Report native accessibility, browser, OCR and vision availability on this machine."""
    session = computer_session(app, headless=_headless())
    caps = session.capabilities()
    out = app.output
    engine = _engine_status(app)
    from highhx.computer.providers import Capability
    from highhx.language.targets import default_registry, user_targets_file

    registry = default_registry(user_file=user_targets_file())
    caps.append(
        Capability(
            f"engine ({engine.get('engine') or 'none'})", bool(engine.get("ok")), str(engine.get("detail") or "")
        )
    )
    known = registry.known()
    targets = f"{len(known['sites'])} sites, {len(known['apps'])} apps, {len(known['places'])} project places"
    caps.append(
        Capability(
            "targets",
            not registry.problems,
            targets + ("; " + "; ".join(registry.problems) if registry.problems else ""),
        )
    )
    out.emit(
        {
            "capabilities": [c.__dict__ for c in caps],
            "browser_running": session.browser.running,
            "engine": engine,
            "targets": known,
        },
        lambda: out.table(
            ["capability", "available", "detail"], [(c.name, "yes" if c.available else "no", c.detail) for c in caps]
        ),
    )
    return 0


def _engine_status(app: App) -> dict[str, Any]:
    """The automation engine in use (C#/.NET when installed, else Python) and what it reports."""
    from highhx.automation.engine.bridge import engine_status
    from highhx.execution.command import CommandSpec

    def runner(argv: list[str], what: str) -> tuple[int, str, str]:
        result = app.engine.run(
            CommandSpec(argv, name=argv[0], timeout=30), action=what, approved=True, echo=False, record=False
        )
        return (0 if result.ok else (result.exit_code or 1)), result.stdout or "", result.stderr or result.error or ""

    return engine_status(runner)


@computer.command("open", short_help="Open a URL in the HighhX browser, or launch an application.")
@click.argument("target")
@pass_app
def computer_open(app: App, target: str) -> int:
    """`highhx computer open https://example.com` · `highhx computer open Calculator`."""
    from highhx.computer.intents import _URL

    session = computer_session(app, headless=_headless())
    try:
        if _URL.match(target) or "://" in target:
            outcome = session.runtime("browser").navigate(
                target
                if "://" in target
                else ("http://" + target if target.startswith(("localhost", "127.")) else "https://" + target)
            )
        else:
            outcome = session.launch(target)
    finally:
        session.close()
    return render_outcome(app, outcome)


@computer.command("observe", short_help="List the controls on screen (role, name, state) and valid actions.")
@click.option(
    "--source",
    type=click.Choice(OBSERVE_SOURCES),
    default="browser",
    show_default=True,
    help="browser (DevTools DOM), desktop (native accessibility, macOS) or screen (local OCR, read-only).",
)
@click.option(
    "--app", "application", metavar="NAME", help="Desktop source: application to inspect (default: frontmost)."
)
@click.option("--actions", is_flag=True, help="Also list the valid action ids.")
@pass_app
def computer_observe(app: App, source: str, application: str | None, actions: bool) -> int:
    """Observe the browser page or the frontmost application semantically."""
    from highhx.computer.model import candidates

    session = computer_session(app, headless=_headless())
    if application:
        session._desktop_app = application
    try:
        observation = session.observe_screen() if source == "screen" else session.runtime(source).observe()
    finally:
        session.close()
    out = app.output
    options = candidates(observation) if actions and source != "screen" else []

    def render() -> None:
        out.markup(
            f"[title]{escape(observation.application)}[/title] {escape(observation.title)} [muted]{escape(observation.url)}[/muted]"
        )
        out.table(
            ["id", "role", "name", "state"],
            [
                (e.id, e.role, e.name, e.label().split(")", 1)[0].split("(", 1)[-1] if "(" in e.label() else "")
                for e in observation.elements
            ],
        )
        if options:
            out.plain()
            out.lines(f"{c.id:<14} {c.description}" for c in options)

    out.emit(
        {
            "application": observation.application,
            "title": observation.title,
            "url": observation.url,
            "elements": [e.to_dict() for e in observation.elements],
            "actions": [c.id for c in options],
        },
        render,
    )
    return 0


def _act(app: App, source: str, verb: str, selector: str | None, text: str | None = None) -> int:
    from highhx.computer.model import Selector

    session = computer_session(app, headless=_headless())
    try:
        runtime = session.runtime(source)
        runtime.observe()
        if selector is None:
            outcome = runtime.act(f"{verb}")
        else:
            roles = None
            if verb == "type":
                from highhx.computer.model import TYPEABLE

                roles = TYPEABLE
            element = runtime.resolve(Selector.parse(selector), role_hint=roles)
            outcome = runtime.act(f"{verb}:{element.id}", text)
    finally:
        session.close()
    return render_outcome(app, outcome)


@computer.command("click", short_help="Click a control by role/name, or a desktop point.")
@click.argument("selector", required=False)
@click.option("--at", "point", metavar="X,Y", help="Desktop: click at this point (desktop points).")
@click.option("--text", metavar="TEXT", help="Desktop: click this text on screen (accessibility, then OCR).")
@click.option("--right", is_flag=True, help="With --at/--text: the right button.")
@click.option("--double", is_flag=True, help="With --at/--text: a double click.")
@SOURCE_OPTION
@pass_app
def computer_click(
    app: App, selector: str | None, point: str | None, text: str | None, right: bool, double: bool, source: str
) -> int:
    """Click the control SELECTOR (e.g. `button:Search`) — or, on the desktop, at a point
    (`--at 640,400`) or on text found on screen (`--text Save`). Sensitive controls, and every
    click at a point, ask first."""
    if point or text:
        inputs: dict[str, Any] = {"button": "right" if right else "left", "count": 2 if double else 1}
        if point:
            inputs["x"], inputs["y"] = _point(point)
        else:
            inputs["text"] = text
        return run_action(app, "computer.click_at", inputs)
    if selector is None:
        raise UsageError("Give a SELECTOR (e.g. button:Save), --at X,Y or --text TEXT.")
    return _act(app, source, "click", selector)


@computer.command("type", short_help="Type text into a field.")
@click.argument("selector")
@click.argument("text", required=False)
@click.option(
    "--from-env", "env_var", metavar="VAR", help="Type the value of this environment variable (never shown or logged)."
)
@SOURCE_OPTION
@pass_app
def computer_type(app: App, selector: str, text: str | None, env_var: str | None, source: str) -> int:
    """Replace the content of the field SELECTOR with TEXT (or $VAR with --from-env)."""
    if env_var:
        text = os.environ.get(env_var)
        if text is None:
            raise UsageError(f"Environment variable {env_var} is not set.")
        app.redactor.add([text])
    if text is None:
        raise UsageError("Give TEXT or --from-env VAR.")
    return _act(app, source, "type", selector, text)


@computer.command("select", short_help="Choose an option in a list.")
@click.argument("selector")
@click.argument("option")
@SOURCE_OPTION
@pass_app
def computer_select(app: App, selector: str, option: str, source: str) -> int:
    """Choose OPTION (value or visible text) in the list SELECTOR."""
    return _act(app, source, "select", selector, option)


@computer.command("press", short_help="Press a key (enter, tab, escape …).")
@click.argument(
    "key",
    type=click.Choice(["enter", "tab", "escape", "backspace", "arrowdown", "arrowup", "pagedown", "pageup", "space"]),
)
@SOURCE_OPTION
@pass_app
def computer_press(app: App, key: str, source: str) -> int:
    """Press KEY in the focused control. Enter in a form counts as submitting it."""
    return _act(app, source, f"press:{key}", None)


@computer.command("scroll", short_help="Scroll up or down.")
@click.argument("direction", type=click.Choice(["up", "down", "left", "right"]))
@click.option("--at", "point", metavar="X,Y", help="Desktop: scroll the mouse wheel at this point.")
@click.option("--amount", type=click.IntRange(1, 20), default=3, show_default=True, help="Desktop: wheel steps.")
@SOURCE_OPTION
@pass_app
def computer_scroll(app: App, direction: str, point: str | None, amount: int, source: str) -> int:
    """Scroll the page or window (the desktop with real wheel events)."""
    if point or source == "desktop":
        inputs: dict[str, Any] = {"source": "desktop", "direction": direction, "amount": amount}
        if point:
            inputs["x"], inputs["y"] = _point(point)
        return run_action(app, "computer.scroll", inputs)
    if direction in ("left", "right"):
        raise UsageError("The browser scrolls up or down; left and right are desktop scrolling (--source desktop).")
    return _act(app, source, f"scroll:{direction}", None)


# ------------------------------------------------ the HighhX Computer Runtime (desktop)
def _point(text: str) -> tuple[int, int]:
    try:
        x, y = (int(v.strip()) for v in text.split(","))
    except ValueError:
        raise UsageError(f"{text!r} is not a point: give X,Y (desktop points, e.g. 640,400).") from None
    return x, y


def run_action(app: App, name: str, inputs: dict[str, Any], render: Any = None) -> int:
    """One computer.* action through the user's action executor (risk, approval, audit, verification)."""
    result = app.user_actions().run(name, inputs)
    out = app.output

    def plain() -> None:
        if render is not None and result.ok:
            render(result.output)
        verdict = {True: "verified", False: "NOT verified", None: ""}[result.verified]
        line = f"{result.summary or result.status}" + (f" ({verdict})" if verdict and result.ok else "")
        if result.ok:
            out.success(line)
        else:
            out.error(f"{name}: {result.error or result.status}")

    out.emit({"action": name, **result.to_dict()}, plain)
    return {"ok": 0, "planned": 0, "cancelled": 130, "timeout": 124, "denied": 6, "blocked": 7}.get(result.status, 1)


@computer.command("protocol", short_help="Print the computer-operations contract (JSON).")
@pass_app
def computer_protocol(app: App) -> int:
    """The automation protocol every engine speaks — operations, arguments, the version that
    introduced each, keys, errors — as JSON. It is also checked in as
    schemas/computer-protocol.json (the source for engines written in other languages)."""
    import json

    from highhx.automation.engine.protocol import describe

    click.echo(json.dumps(describe(), indent=2, sort_keys=True))
    return 0


@computer.command("screenshot", short_help="Capture the screen or one window to a PNG.")
@click.option("--window", "window", type=int, metavar="ID", help="Only this window (ids from `computer windows`).")
@pass_app
def computer_screenshot(app: App, window: int | None) -> int:
    """Save a screenshot in HighhX's screenshots folder and print its path. macOS needs Screen
    Recording permission for your terminal; HighhX says so instead of saving a blank image."""
    return run_action(app, "computer.screenshot", {"window": window} if window is not None else {})


@computer.command("windows", short_help="List windows (or, with --apps, running applications).")
@click.option("--app", "application", metavar="NAME", help="Only this application's windows.")
@click.option("--apps", "list_apps", is_flag=True, help="List running applications instead.")
@pass_app
def computer_windows(app: App, application: str | None, list_apps: bool) -> int:
    """Windows front to back with their ids and bounds (for `click --at`, `window`, `screenshot --window`)."""
    out = app.output
    if list_apps:
        return run_action(
            app,
            "computer.apps",
            {},
            lambda o: out.table(
                ["application", "pid", "front"],
                [(a["name"], a["pid"], "yes" if a["frontmost"] else "") for a in o["apps"]],
            ),
        )
    return run_action(
        app,
        "computer.windows",
        {"app": application} if application else {},
        lambda o: out.table(
            ["id", "application", "title", "x", "y", "width", "height"],
            [(w["id"], w["app"], w["title"], w["x"], w["y"], w["width"], w["height"]) for w in o["windows"]],
        ),
    )


@computer.command("at", short_help="The accessibility element at a desktop point.")
@click.argument("point", metavar="X,Y")
@pass_app
def computer_at(app: App, point: str) -> int:
    """What is at X,Y — application, role, name and bounds (read-only)."""
    x, y = _point(point)
    return run_action(app, "computer.element_at", {"x": x, "y": y})


@computer.command("move", short_help="Move the pointer (hover).")
@click.argument("point", metavar="X,Y")
@pass_app
def computer_move(app: App, point: str) -> int:
    """Move the pointer to X,Y (hovering) and check that it arrived."""
    x, y = _point(point)
    return run_action(app, "computer.move", {"x": x, "y": y})


@computer.command("drag", short_help="Drag from one desktop point to another.")
@click.argument("start", metavar="X1,Y1")
@click.argument("end", metavar="X2,Y2")
@click.option("--right", is_flag=True, help="With the right button.")
@pass_app
def computer_drag(app: App, start: str, end: str, right: bool) -> int:
    """Press at X1,Y1, move to X2,Y2 and release (asks first)."""
    (x1, y1), (x2, y2) = _point(start), _point(end)
    inputs = {"from_x": x1, "from_y": y1, "to_x": x2, "to_y": y2, "button": "right" if right else "left"}
    return run_action(app, "computer.drag", inputs)


@computer.command("menu", short_help='Choose an application menu item, e.g. "File > Save".')
@click.argument("application", metavar="APP")
@click.argument("path")
@pass_app
def computer_menu(app: App, application: str, path: str) -> int:
    """Choose PATH (items separated by >) in APP's menu bar."""
    return run_action(app, "computer.menu", {"app": application, "path": path})


@computer.command("window", short_help="Move and resize an application's window.")
@click.argument("application", metavar="APP", required=False)
@click.option("--id", "window", type=int, help="The window id (from `computer windows`) instead of APP.")
@click.option("--frame", required=True, metavar="X,Y,WIDTH,HEIGHT", help="The new frame in desktop points.")
@pass_app
def computer_window(app: App, application: str | None, window: int | None, frame: str) -> int:
    """Move and resize APP's front window (or --id WINDOW) to --frame, and check the new frame."""
    try:
        x, y, width, height = (int(v.strip()) for v in frame.split(","))
    except ValueError:
        raise UsageError("--frame needs X,Y,WIDTH,HEIGHT, e.g. 0,25,1200,800.") from None
    target: dict[str, Any] = {"window": window} if window is not None else {"app": application}
    if window is None and not application:
        raise UsageError("Give APP or --id WINDOW.")
    return run_action(app, "computer.window", {**target, "x": x, "y": y, "width": width, "height": height})


@computer.command("quit", short_help="Ask an application to quit.")
@click.argument("application", metavar="APP")
@pass_app
def computer_quit(app: App, application: str) -> int:
    """Ask APP to quit (it may ask to save first) and check that it did."""
    return run_action(app, "computer.quit", {"app": application})


@computer.command("clipboard", short_help="Read the clipboard, or replace it with --set.")
@click.option("--set", "text", metavar="TEXT", help="Replace the clipboard with TEXT.")
@pass_app
def computer_clipboard(app: App, text: str | None) -> int:
    """Print the clipboard's text, or replace it with --set TEXT (read back to check). Asks first:
    the clipboard often holds private data."""
    if text is not None:
        return run_action(app, "computer.clipboard_write", {"text": text})
    return run_action(app, "computer.clipboard_read", {}, lambda o: app.output.plain(o.get("text") or ""))


@computer.command("run", short_help="Run a deterministic automation flow (YAML).")
@click.argument("flow_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@SOURCE_OPTION
@pass_app
def computer_run(app: App, flow_file: Path, source: str) -> int:
    """Run the steps in FLOW_FILE (open, click, type, press, select, scroll, expect,
    launch, wait). Each step is resolved deterministically, safety-checked,
    executed and verified; the flow stops at the first failing step."""
    from highhx.computer.flows import FlowRunner, load_flow

    flow = load_flow(flow_file)
    session = computer_session(app, headless=_headless())
    out = app.output

    def on_step(number: int, label: str, ok: bool, problem: str) -> None:
        (out.success if ok else out.error)(f"{number}. {label}" + (f" — {problem}" if problem else ""))

    try:
        with app.engine.operation("flow", flow.name):
            result = FlowRunner(session.runtime(source), launch=session.launch).run(flow, on_step=on_step)
    finally:
        session.close()
    out.emit(
        result.to_dict(),
        lambda: (out.success if result.ok else out.error)(f"Flow {flow.name}: {'passed' if result.ok else 'failed'}"),
    )
    return 0 if result.ok else 1


@computer.command("task", short_help="Work toward a goal: plan, act, verify, recover (Task IR).")
@click.argument("request", nargs=-1)
@click.option(
    "--ir",
    "ir_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Run this Task IR (JSON or YAML) instead of a request.",
)
@click.option("--show-ir", is_flag=True, help="Print the Task IR before running it.")
@click.option("--schema", is_flag=True, help="Print the Task IR JSON Schemas and exit.")
@click.option("--max-steps", type=click.IntRange(1, 200), help="At most this many actions.")
@click.option("--timeout", type=click.FloatRange(1, 3600), help="Give up after this many seconds.")
@pass_app
def computer_task(
    app: App,
    request: tuple[str, ...],
    ir_file: Path | None,
    show_ir: bool,
    schema: bool,
    max_steps: int | None,
    timeout: float | None,
) -> int:
    """Work toward a goal in the HighhX browser, step by step:

    \b
      TASK → PLAN → OBSERVE → ACTION → RESULT → VERIFY → (RECOVERY) → … → FINAL

    The request becomes Task IR (validated JSON): on HighhX Free from the deterministic
    resolver, on HighhX Pro — for tasks nobody programmed — from the AI planner, which then
    discovers the site from its accessibility tree. Each action is a generic primitive
    (navigate, click, type, read, …), checked by the safety policy, executed, verified and
    audited; failures are recovered or replanned, never blindly repeated.

    \b
      highhx computer task "open YouTube and play lofi"
      highhx computer task --ir task.json
      highhx computer task --schema
    """
    import json

    from highhx.goals import runner
    from highhx.goals.conditions import check
    from highhx.goals.executor import GoalExecutor
    from highhx.goals.ir import json_schemas
    from highhx.goals.log import TaskLog
    from highhx.goals.loop import GoalLoop
    from highhx.goals.planner import Planner, ScriptedPlanner
    from highhx.goals.state import CANCELLED, COMPLETED, NEEDS_USER
    from highhx.utils.paths import user_data_dir

    out = app.output
    if schema:
        out.emit(json_schemas(), lambda: out.console.print_json(json.dumps(json_schemas())))
        return 0
    prepared = runner.prepare(app, " ".join(request), ir_file, app.ctx.cancel)
    task = runner.with_limits(prepared.task, max_steps=max_steps, timeout=timeout)
    if show_ir or app.options.dry_run:
        if not out.json_mode:
            out.note(f"Task IR ({prepared.source}):")
            out.console.print_json(json.dumps(task.to_dict()))
    if app.options.dry_run:
        out.emit(
            {"ok": True, "dry_run": True, "source": prepared.source, "task": task.to_dict()},
            lambda: out.note("Dry run: nothing was executed."),
        )
        return 0
    styles = {True: "ok", False: "fail", None: "dim"}

    def render(entry: Any) -> None:
        if out.json_mode or out.quiet:
            return
        mark = "" if entry.ok is None else ("✓ " if entry.ok else "✗ ")
        style = (
            styles[entry.ok]
            if entry.kind in ("VERIFY", "FINAL", "RESULT")
            else "bold"
            if entry.kind in ("TASK", "ACTION")
            else "dim"
        )
        out.console.print(f"[bold]{entry.kind + ':':<10}[/bold][{style}]{mark}{escape(entry.message)}[/{style}]")

    agent = prepared.source == "model" or prepared.replanner is not None
    session = computer_session(app, headless=_headless(), agent=agent)
    try:
        runtime = session.runtime("browser")
        executor = GoalExecutor(runtime, screenshots=user_data_dir() / "screenshots")
        planner: Planner = (
            prepared.replanner
            if task.dynamic and prepared.replanner is not None
            else ScriptedPlanner(task, lambda condition, page: check(condition, page, runtime))
        )
        log = TaskLog(render, redact=app.redactor.redact)
        loop = GoalLoop(executor, planner, log, replanner=prepared.replanner, log_dir=user_data_dir() / "tasks")
        with app.engine.operation("task", task.goal[:60]):
            state = loop.run(task)
    finally:
        session.close()
    log_file = str(log.file) if log.file is not None else None
    data = {
        "ok": state.status == COMPLETED,
        **state.to_dict(),
        "log": [e.to_dict() for e in log.entries],
        "log_file": log_file,
    }
    out.emit(data, lambda: out.note(f"log: {log_file}") if log_file else None)
    return {COMPLETED: 0, NEEDS_USER: 2, CANCELLED: 130}.get(state.status, 1)


@computer.group("browser", short_help="Start or stop the HighhX-controlled browser.")
def browser() -> None:
    """The HighhX browser uses its own profile (never your personal one) and a DevTools
    port bound to 127.0.0.1."""


@browser.command("start", short_help="Start the browser (reused by later commands).")
@pass_app
def browser_start(app: App) -> int:
    """Start Chrome/Chromium/Edge/Brave for HighhX automation."""
    session = computer_session(app, headless=_headless())
    state: dict[str, Any] = session.browser.start(cancel=app.ctx.cancel)
    app.output.emit(
        state,
        lambda: app.output.success(f"Browser running (pid {state['pid']}, DevTools on 127.0.0.1:{state['port']})"),
    )
    return 0


@browser.command("stop", short_help="Close the HighhX browser.")
@pass_app
def browser_stop(app: App) -> int:
    """Close the HighhX browser and forget its session."""
    stopped = computer_session(app).browser.stop()
    app.output.emit(
        {"stopped": stopped}, lambda: app.output.info("Browser stopped." if stopped else "The browser was not running.")
    )
    return 0

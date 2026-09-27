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


def computer_session(app: App, *, headless: bool | None = None) -> ComputerSession:
    """A session whose actions are the user's own (Actor.USER), gated, confirmed and audited."""
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
    return ComputerSession(gate, actor=Actor.USER, cancel=app.ctx.cancel, headless=headless)


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
    out.emit(
        {"capabilities": [c.__dict__ for c in caps], "browser_running": session.browser.running},
        lambda: out.table(
            ["capability", "available", "detail"], [(c.name, "yes" if c.available else "no", c.detail) for c in caps]
        ),
    )
    return 0


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


@computer.command("click", short_help="Click a control by role/name.")
@click.argument("selector")
@SOURCE_OPTION
@pass_app
def computer_click(app: App, selector: str, source: str) -> int:
    """Click the control SELECTOR (e.g. `button:Search`). Sensitive controls ask first."""
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
@click.argument("direction", type=click.Choice(["up", "down"]))
@SOURCE_OPTION
@pass_app
def computer_scroll(app: App, direction: str, source: str) -> int:
    """Scroll the page or window."""
    return _act(app, source, f"scroll:{direction}", None)


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

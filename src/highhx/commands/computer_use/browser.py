"""highhx browser — record browser workflows and replay them with self-healing."""

from __future__ import annotations

import time
from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import UsageError


@click.group("browser", cls=DefaultGroup, default_command="workflows", short_help="Record and replay browser workflows (self-healing).")
def browser() -> None:
    """Record what you do in the HighhX browser as semantic steps (role, name, test id, href,
    text, position — never only coordinates), then replay it: every target is found again on the
    current page, every action is approved and verified, and selectors that drifted are healed
    and saved. Secret fields are never recorded."""


def _store(app: App) -> Any:
    from highhx.computer.recorder import WorkflowStore

    return WorkflowStore.for_app(app)


@browser.command("record", short_help="Record a workflow in the HighhX browser.")
@click.argument("name")
@click.option("--url", help="Start here.")
@click.option("--duration", type=float, help="Stop after this many seconds (default: Ctrl-C).")
@pass_app
def browser_record(app: App, name: str, url: str | None, duration: float | None) -> int:
    """Open the HighhX browser and record clicks, typing, selections and Enter until Ctrl-C
    (or --duration). Nothing is clicked for you while recording."""
    from highhx.actions.catalog import catalog_for
    from highhx.actions.executor import ActionExecutor
    from highhx.commands.computer.main import computer_session
    from highhx.computer.recorder import BrowserRecorder

    store = _store(app)
    store.path(name)  # validates the name before the browser opens
    session = computer_session(app, headless=False)
    recorder = BrowserRecorder(session.browser, cancel=app.ctx.cancel)
    out = app.output
    try:
        if url:  # opened like any page: classified, policy-checked, audited
            executor = ActionExecutor(app, session.gate, actor=session.actor, catalog=catalog_for(app), computer=lambda: session)
            opened = executor.run("browser.open", {"url": url})
            if not opened.ok:
                raise UsageError(f"Could not open {url}: {opened.error or opened.status}")
        recorder.start(url)
        out.info(f"Recording {name!r} — use the browser, then press Ctrl-C here to stop.")
        started = time.monotonic()
        seen = 0
        try:
            while duration is None or time.monotonic() - started < duration:
                count = recorder.poll(0.3)
                for event in recorder.events[seen:count]:
                    element = event.get("element") or {}
                    out.note(f"  {event.get('kind')}: {element.get('role', '')} {element.get('name', event.get('url', ''))!r}")
                seen = count
                if app.ctx.cancel.cancelled:
                    break
        except KeyboardInterrupt:
            pass
        recorder.stop()
    finally:
        session.close()
    workflow = recorder.workflow(name, url or "")
    if not workflow.steps:
        raise UsageError("Nothing was recorded.", hint="Click or type in the HighhX browser while recording.")
    path = store.save(workflow)

    def render() -> None:
        out.success(f"Recorded {len(workflow.steps)} step(s) → {path}")
        _steps(app, workflow)

    out.emit(workflow.to_dict(), render)
    return 0


def _steps(app: App, workflow: Any) -> None:
    rows = [(i, s.action, s.target.get("role", ""), s.target.get("label", "") or s.parameters.get("url", ""), "→ " + ", ".join(s.verify) if isinstance(s.verify, dict) else "") for i, s in enumerate(workflow.steps, 1)]
    app.output.table(["#", "action", "role", "target", "check"], rows)
    if workflow.variables:
        app.output.note(f"needs at replay: {', '.join(workflow.variables)} (--var NAME=…)")


@browser.command("replay", short_help="Replay a workflow (self-healing).")
@click.argument("name")
@click.option("--var", "variables", multiple=True, metavar="NAME=VALUE", help="Values for secret fields (or HIGHHX_VAR_NAME).")
@click.option("--save-heals/--no-save-heals", default=True, help="Save healed selectors back to the workflow.")
@click.option("--live/--no-live", default=True, help="Show the live dashboard.")
@pass_app
def browser_replay(app: App, name: str, variables: tuple[str, ...], save_heals: bool, live: bool) -> int:
    """Replay NAME: targets are grounded on the current page (accessibility → DOM → text → OCR →
    vision → coordinates); drifted selectors are healed and saved."""
    from highhx import computer_use
    from highhx.computer.recorder import replay, variables_from

    store = _store(app)
    workflow = store.load(name)
    values = variables_from(variables, workflow.variables)
    trajectories, traces = computer_use.stores(app)
    with computer_use.session(app, live=live and app.output.human, source="browser-replay") as handle:
        report = replay(handle.executor, workflow, variables=values, workflows=store, trajectories=trajectories, traces=traces, save_heals=save_heals)
    out = app.output

    def render() -> None:
        (out.success if report.status == "completed" else out.error)(f"{name}: {report.status} — {report.summary}")
        for heal in report.healed:
            out.note(f"  healed step {heal['step']}: {heal['was']!r} → {heal['now']!r} via {heal['strategy']} ({heal['confidence']})")
        if report.healed and not report.saved:
            out.note("  (not saved: --no-save-heals)")
        out.note(f"  trace: highhx trace show {report.task_id}")

    out.emit(report.to_dict(), render)
    return 0 if report.status == "completed" else 1


@browser.command("heal", short_help="Replay a workflow and save every healed selector.")
@click.argument("name")
@click.option("--var", "variables", multiple=True, metavar="NAME=VALUE")
def browser_heal(name: str, variables: tuple[str, ...]) -> int:
    """The same as `replay --save-heals`: run it against the site as it is now and refresh the
    selectors that drifted."""
    result: int = click.get_current_context().invoke(browser_replay, name=name, variables=variables, save_heals=True, live=False)
    return result


@browser.command("workflows", short_help="Recorded workflows.")
@pass_app
def browser_workflows(app: App) -> int:
    """List recorded browser workflows."""
    store = _store(app)
    items = [store.load(n) for n in store.names()]
    data = [{"name": w.name, "steps": len(w.steps), "start_url": w.start_url, "heals": w.heals, "variables": w.variables} for w in items]
    app.output.emit(data, lambda: app.output.table(["name", "steps", "start", "heals"], [(d["name"], d["steps"], d["start_url"], d["heals"]) for d in data]))
    return 0


@browser.command("show", short_help="The steps of a workflow.")
@click.argument("name")
@pass_app
def browser_show(app: App, name: str) -> int:
    """Each step with its target and check, and the heal history."""
    workflow = _store(app).load(name)
    app.output.emit(workflow.to_dict(), lambda: _steps(app, workflow))
    return 0


@browser.command("delete", short_help="Delete a workflow.")
@click.argument("name")
@pass_app
def browser_delete(app: App, name: str) -> int:
    """Delete the recorded workflow NAME."""
    store = _store(app)
    store.load(name)
    store.delete(name)
    app.output.emit({"deleted": name}, lambda: app.output.success(f"deleted {name}"))
    return 0

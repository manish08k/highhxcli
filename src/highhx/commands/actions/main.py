"""highhx actions — the action catalog: list, show, plan (preview) and run actions."""

from __future__ import annotations

import json

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.core.errors import UsageError


def _inputs(pairs: tuple[str, ...], raw_json: str | None) -> dict[str, object]:
    from highhx.agent.repl import parse_inputs

    try:
        if raw_json:
            return parse_inputs(raw_json)
        return parse_inputs(" ".join(_quote(p) for p in pairs))
    except ValueError as exc:
        raise UsageError(str(exc)) from None


def _quote(pair: str) -> str:
    import shlex

    return shlex.quote(pair)


@click.group(
    "actions", cls=DefaultGroup, default_command="list", short_help="The action catalog: list, show, plan, run."
)
def actions() -> None:
    """Every capability HighhX executes is an action with an input schema, a risk level,
    required permissions, a timeout, a retry policy (idempotent actions only), verification
    and — where possible — a compensation for rollback. Plain-language requests, workflow
    steps and the AI agent all run actions through the same executor and approvals."""


@actions.command("list", short_help="All actions, by category, with their risk.")
@click.argument("category", required=False)
@pass_app
def list_actions(app: App, category: str | None) -> int:
    """List the actions (optionally one CATEGORY: project, filesystem, git, package, docker,
    database, service, browser, computer, deployment, security, workflow, shell) with their
    risk floor, retries (idempotent actions only) and whether they can be undone."""
    from highhx.actions.catalog import default_catalog

    catalog = default_catalog()
    specs = [s for s in catalog if category is None or s.category == category]
    if not specs:
        raise UsageError(f"No action category '{category}'.", hint=f"Categories: {', '.join(catalog.categories())}")
    out = app.output
    out.emit(
        {"actions": [s.to_dict() for s in specs]},
        lambda: out.table(
            ["action", "risk", "retry", "undo", "description"],
            [
                (
                    s.name,
                    s.risk.label,
                    str(s.retries.attempts) if s.idempotent else "-",
                    "yes" if s.compensate else "-",
                    s.description,
                )
                for s in specs
            ],
        ),
    )
    return 0


@actions.command("show", short_help="One action's schema, risk, permissions and semantics.")
@click.argument("name")
@pass_app
def show_action(app: App, name: str) -> int:
    """Show NAME's contract: input schema, outputs, risk floor, kind, permissions, timeout,
    retry policy, verification, compensation and the plan feature the AI agent needs."""
    from highhx.actions.catalog import default_catalog

    spec = default_catalog().get(name)
    if spec is None:
        raise UsageError(f"Unknown action '{name}'.", hint="See `highhx actions list`.")
    data = spec.to_dict()
    out = app.output
    out.emit(data, lambda: out.print(json.dumps(data, indent=2)))
    return 0


@actions.command("plan", short_help="Preview an action: effective risk and whether it asks.")
@click.argument("name")
@click.argument("inputs", nargs=-1)
@click.option("--with-json", "raw_json", metavar="JSON", help="Inputs as one JSON object.")
@pass_app
def plan_action(app: App, name: str, inputs: tuple[str, ...], raw_json: str | None) -> int:
    """Validate NAME with INPUTS (key=value …) and rate it — the classifier's verdict on the
    concrete action, the catalog's floor and the approval rule — without running anything."""
    planned = app.user_actions().plan(name, _inputs(inputs, raw_json))
    out = app.output
    out.emit(planned.to_dict(), lambda: out.lines(planned.preview()))
    return 0


@actions.command("run", short_help="Run an action (with its approvals).")
@click.argument("name")
@click.argument("inputs", nargs=-1)
@click.option("--with-json", "raw_json", metavar="JSON", help="Inputs as one JSON object.")
@pass_app
def run_action(app: App, name: str, inputs: tuple[str, ...], raw_json: str | None) -> int:
    """Run NAME with INPUTS (key=value …). Medium and high risk actions ask (or run with
    --yes); critical ones need a typed confirmation in a terminal; blocked ones never run."""
    result = app.user_actions().run(name, _inputs(inputs, raw_json))
    out = app.output
    out.emit(
        {"action": name, **result.to_dict()},
        lambda: (out.success if result.ok else out.error)(
            f"{name}: {result.summary or result.status}"
            + (f" — {result.error}" if result.error and not result.ok else "")
        ),
    )
    return {"ok": 0, "planned": 0, "cancelled": 130, "timeout": 124, "denied": 6, "blocked": 7}.get(result.status, 1)

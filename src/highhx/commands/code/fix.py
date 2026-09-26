"""highhx fix"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.execution.command import CommandSpec


@click.command("fix", short_help="Apply automatic fixes (formatter + auto-fixable lint).")
@pass_app
def fix(app: App) -> int:
    """Run commands.fix and then commands.format (configured or detected, e.g.
    `ruff check --fix` + `ruff format`, `dart fix --apply` + `dart format`)."""
    commands = app.commands()
    selected = [(kind, commands[kind]) for kind in ("fix", "format") if commands.get(kind)]
    if not selected:
        raise NotFoundError(
            "No fix or format command configured or detected.",
            hint="Set commands.fix / commands.format in .highhx/config.yaml.",
        )
    results = []
    with app.engine.operation("fix", "fix"):
        for kind, command in selected:
            result = app.engine.run(
                CommandSpec(command, cwd=app.root, name=kind), action=f"{kind}: {command}", policy_action=kind
            )
            results.append(
                {"step": kind, "command": command, "status": str(result.status), "exit_code": result.exit_code}
            )
            if not result.ok and not result.dry_run:
                break
    ok = all(r["status"] in ("success", "skipped") for r in results)
    out = app.output
    out.emit(
        {"ok": ok, "steps": results},
        lambda: out.outcomes((r["status"] in ("success", "skipped"), f"{r['step']}: {r['command']}") for r in results),
    )
    return 0 if ok else 1

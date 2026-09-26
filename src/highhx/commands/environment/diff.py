"""highhx env diff"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("diff", short_help="Compare two profiles (secret values masked).")
@click.argument("left")
@click.argument("right")
@pass_app
def diff(app: App, left: str, right: str) -> int:
    """Show variables that exist only in LEFT or RIGHT, or differ between them."""
    app.require_project()
    result = app.environment.diff(left, right)
    out = app.output

    def render() -> None:
        out.heading(f"{left} ↔ {right}")
        rows = [(k, "✓", "") for k in result.only_left] + [(k, "", "✓") for k in result.only_right]
        if rows:
            out.table(["variable", f"only in {left}", f"only in {right}"], rows)
        if result.changed:
            out.table(
                ["variable", left, right],
                [(c["name"], c["left"], c["right"]) for c in result.changed],
                title="Different values",
            )
        out.note(f"{result.same} identical variable(s)")

    out.emit(result.to_dict(), render)
    return 0

"""highhx plugin list"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("list", short_help="Installed plugins and their status.")
@pass_app
def list_plugins(app: App) -> int:
    """List installed plugins (project and user), their status, whether their code is loaded,
    what they contribute and why code may be blocked."""
    registry = app.plugins
    rows = [p.to_dict() for p in registry.plugins]
    out = app.output

    def render() -> None:
        if rows:
            out.table(
                ["plugin", "version", "scope", "status", "contributes", "note"],
                [
                    (
                        r["name"],
                        r["version"],
                        r["scope"],
                        r["status"] + (" +code" if r["code_loaded"] else ""),
                        ", ".join(
                            filter(
                                None,
                                [
                                    f"{len(r['commands'])} cmd" if r["commands"] else "",
                                    "workflows" if r["workflows"] else "",
                                    "templates" if r["templates"] else "",
                                    "detectors" if r["detectors"] else "",
                                ],
                            )
                        )
                        or "-",
                        r["problem"] or "",
                    )
                    for r in rows
                ],
            )
        else:
            out.info("No plugins installed. Find some with `highhx plugin search <term>`.")
        for error in registry.errors:
            out.warn(error)

    out.emit({"plugins": rows, "errors": registry.errors}, render)
    return 0

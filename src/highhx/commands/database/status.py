"""highhx db status"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("status", short_help="Connectivity and migration state.")
@pass_app
def status(app: App) -> int:
    """Check that the database from the active environment profile is reachable and show
    which migrations are applied, pending, or were modified after being applied."""
    app.require_project()
    st = app.database.status()
    out = app.output

    def render() -> None:
        (out.success if st.reachable else out.error)(
            f"{st.url}: {st.message}" + (f" (server {st.server_version})" if st.server_version else "")
        )
        if st.missing_tools:
            out.warn(f"missing tools: {', '.join(st.missing_tools)}")
        m = st.migrations or {}
        if "managed_by" in m:
            out.info(f"migrations managed by: {m['managed_by']}")
        elif "error" in m:
            out.warn(m["error"])
        elif m:
            out.kv(
                {
                    "applied": len(m["applied"]),
                    "pending": ", ".join(m["pending"]) or "none",
                    "modified after apply": ", ".join(m["changed"]) or "none",
                },
                title="Migrations",
            )

    out.emit(st.to_dict(), render)
    return 0 if st.reachable else 1

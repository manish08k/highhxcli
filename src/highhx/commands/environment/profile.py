"""highhx env profile"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app


@click.command("profile", short_help="List profiles or switch the active profile.")
@click.argument("name", required=False)
@pass_app
def profile(app: App, name: str | None) -> int:
    """Without NAME, list profiles. With NAME, make it the active profile
    (switching to a protected profile such as production needs confirmation).
    HIGHHX_ENV overrides the stored choice."""
    app.require_project()
    manager = app.environment
    out = app.output
    if name is None:
        active = manager.active_profile()
        names = manager.profile_names()
        rows = [
            {
                "name": n,
                "active": n == active,
                "protected": manager.is_protected(n),
                "files": manager.spec.profile(n).files,
            }
            for n in names
        ]
        table = [
            (
                n,
                "●" if n == active else "",
                "yes" if manager.is_protected(n) else "",
                ", ".join(manager.spec.profile(n).files),
            )
            for n in names
        ]
        out.emit(
            {"active": active, "profiles": rows}, lambda: out.table(["profile", "active", "protected", "files"], table)
        )
        return 0
    manager.require_profile(name)
    app.engine.approve(
        f"Switch active environment profile to '{name}'", manager.switch_risk(name), policy_action="env:profile"
    )
    if not app.options.dry_run:
        manager.switch(name)
    out.emit({"active": name}, lambda: out.success(f"Active profile: {name}"))
    return 0

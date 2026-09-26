"""highhx clean"""

from __future__ import annotations

import click

from highhx.approvals.risk import RiskLevel
from highhx.building.cleanup import clean_targets
from highhx.commands import App, pass_app
from highhx.execution.command import CommandSpec
from highhx.utils.filesystem import human_size, path_size, remove_path


@click.command("clean", short_help="Remove build outputs and caches.")
@pass_app
def clean(app: App) -> int:
    """Delete regenerable build output (dist/, build/, target/, __pycache__,
    .pytest_cache …) inside the project only. Runs commands.clean if configured."""
    configured = app.config.commands.get("clean")
    out = app.output
    if configured:
        result = app.engine.run(
            CommandSpec(configured, cwd=app.root, name="clean"), action=f"Clean: {configured}", policy_action="clean"
        )
        out.emit(result.to_dict(include_output=False))
        return 0 if result.ok or result.dry_run else 1
    targets = clean_targets(app.root)
    rows = [(p.relative_to(app.root).as_posix(), path_size(p)) for p in targets]
    if not rows:
        out.emit({"removed": []}, lambda: out.info("Nothing to clean."))
        return 0
    total = sum(size for _, size in rows)
    app.engine.approve(
        f"Delete {len(rows)} build output path(s) ({human_size(total)})",
        RiskLevel.NORMAL,
        details=[r for r, _ in rows[:30]],
        policy_action="clean",
    )
    if not app.options.dry_run:
        with app.engine.operation("clean", "build-outputs"):
            for path in targets:
                if path.exists():
                    remove_path(path)
    verb = "Would remove" if app.options.dry_run else "Removed"

    def render() -> None:
        out.success(f"{verb} {len(rows)} path(s), {human_size(total)}")
        for rel, _ in rows:
            out.detail(rel)

    out.emit(
        {"removed": [r for r, _ in rows], "bytes": total, "dry_run": app.options.dry_run},
        render,
    )
    return 0

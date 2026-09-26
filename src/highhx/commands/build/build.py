"""highhx build"""

from __future__ import annotations

import click

from highhx.commands import App, exit_code_for, pass_app
from highhx.utils.filesystem import human_size


def render_outcome(app: App, outcome, kind: str) -> None:  # type: ignore[no-untyped-def]
    out = app.output
    if outcome.result.dry_run:
        return
    if not outcome.result.ok:
        out.error(f"{kind.capitalize()} failed (exit code {outcome.result.exit_code})")
        return
    out.success(f"{kind.capitalize()} completed in {outcome.result.duration:.1f}s")
    new = set(outcome.new_artifacts)
    rows = [
        (a.path + (" (new)" if a.path in new else ""), human_size(a.size), a.sha256[:12]) for a in outcome.artifacts
    ]
    if rows:
        out.table(["artifact", "size", "sha256"], rows)


@click.command("build", short_help="Build the project.")
@click.option("--workflow/--no-workflow", default=True, help="Use .highhx/workflows/build.yaml when it exists.")
@pass_app
def build(app: App, workflow: bool) -> int:
    """Run the build workflow (if present) or the build command, then record
    artifacts with SHA-256 checksums."""
    if workflow and app.initialized and "build" in app.workflow_loader:
        result = app.workflows.run("build")
        if result.ok and not result.dry_run:
            from highhx.building.artifacts import find_artifacts, write_manifest

            write_manifest(app.paths.state_dir, find_artifacts(app.root))
        app.output.emit(result.to_dict())
        return 0 if result.ok else 1
    outcome = app.builder.build()
    app.output.emit(outcome.to_dict(), lambda: render_outcome(app, outcome, "build"))
    return exit_code_for(outcome.result)

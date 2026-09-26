"""highhx security report"""

from __future__ import annotations

import json
from pathlib import Path

import click

from highhx.approvals.risk import RiskLevel
from highhx.commands import App, pass_app
from highhx.commands.security.main import run_scan
from highhx.security.scanner import ALL_CHECKS
from highhx.utils.filesystem import atomic_write_text


@click.command("report", short_help="Write a full security report (markdown or JSON).")
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]), default="markdown", show_default=True)
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    help="File to write (default: print).",
)
@pass_app
def report(app: App, fmt: str, output_path: Path | None) -> int:
    """Run every check and write the complete report as Markdown or JSON to --output
    (or print it). Overwriting an existing file asks for confirmation unless --force."""
    result = run_scan(app, ALL_CHECKS)
    text = (
        result.to_markdown(app.config.project_name or app.root.name)
        if fmt == "markdown"
        else json.dumps(result.to_dict(), indent=2) + "\n"
    )
    if output_path is None:
        if app.options.json:
            app.output.json(result.to_dict())
        else:
            click.echo(text, nl=False)
        return 0
    target = output_path if output_path.is_absolute() else app.start_dir / output_path
    if target.exists() and not app.options.force:
        app.engine.approve(f"Overwrite {target.name}", RiskLevel.DANGEROUS, policy_action="security:report")
    if not app.options.dry_run:
        atomic_write_text(target, text)
    app.output.emit({"written": str(target), "counts": result.counts()}, lambda: app.output.success(f"Wrote {target}"))
    return 0

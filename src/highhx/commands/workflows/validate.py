"""highhx workflow validate"""

from __future__ import annotations

from pathlib import Path

import click

from highhx.commands import App, pass_app
from highhx.workflows.validator import validate_file


@click.command("validate", short_help="Validate workflows before running them.")
@click.argument("names", nargs=-1)
@click.option("--strict", is_flag=True, help="Treat warnings as errors.")
@click.option("--no-tool-check", is_flag=True, help="Don't warn about commands missing from PATH.")
@pass_app
def validate(app: App, names: tuple[str, ...], strict: bool, no_tool_check: bool) -> int:
    """Check schema, duplicate ids, missing/circular dependencies, conditions,
    variable references, reusable workflows, unparsable commands and risky
    commands without approval. Validates every workflow when no NAMES are given."""
    loader = app.workflow_loader
    paths: list[Path] = [loader.find(n) for n in names] if names else [r.path for r in loader.list()]
    reports = [validate_file(p, loader=loader, check_tools=not no_tool_check, base_dir=app.root) for p in paths]
    failed = [r for r in reports if r.errors or (strict and r.warnings)]
    out = app.output

    def render() -> None:
        if not reports:
            out.info("No workflows found.")
        for report in reports:
            label = Path(report.source).name if report.source else report.workflow
            if report.errors:
                out.error(f"{label}: {len(report.errors)} error(s)")
                for error in report.errors:
                    out.plain(f"    {error}")
            else:
                out.success(f"{label}: valid")
            for warning in report.warnings:
                out.warn(f"    {warning}")

    out.emit({"valid": not failed, "workflows": [r.to_dict() for r in reports]}, render)
    return 8 if failed else 0

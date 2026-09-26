"""highhx workflow create"""

from __future__ import annotations

import click

from highhx.approvals.risk import RiskLevel
from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError, ValidationError
from highhx.project.initializer import template_context
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.validation import is_identifier
from highhx.workflows.templates import BLANK_WORKFLOW, TemplateLibrary
from highhx.workflows.validator import validate_file


@click.command("create", short_help="Create a workflow from a template.")
@click.argument("name")
@click.option(
    "--template",
    "-t",
    metavar="NAME",
    help="Template (dev, test, build, ci, release, deploy, rollback …). Default: blank.",
)
@click.option("--list-templates", is_flag=True, help="Show available templates.")
@pass_app
def create(app: App, name: str, template: str | None, list_templates: bool) -> int:
    """Create .highhx/workflows/NAME.yaml, filled with this project's detected commands."""
    app.require_project()
    library = TemplateLibrary(app.plugins.template_dirs)
    templates = library.workflow_templates(app.profile.template)
    out = app.output
    if list_templates:
        out.emit({"templates": sorted(templates)}, lambda: out.plain(", ".join(sorted(templates))))
        return 0
    if not is_identifier(name):
        raise ValidationError(f"Invalid workflow name '{name}'.", hint="Use letters, digits, '-' and '_'.")
    target = app.paths.workflows_dir / f"{name}.yaml"
    if template:
        if template not in templates:
            raise NotFoundError(f"Unknown template '{template}'.", hint=f"Templates: {', '.join(sorted(templates))}")
        text = library.render_workflow(template, template_context(app.profile, app.commands()), app.profile.template)
        if text is None:
            raise ValidationError(
                f"Template '{template}' has no steps for this project (no matching commands detected)."
            )
        text = text.replace(f"name: {template}", f"name: {name}", 1)
    else:
        text = BLANK_WORKFLOW.format(
            name=name, description="Describe what this workflow does.", command="echo hello from highhx"
        )
    if target.exists():
        if not app.options.force:
            raise ValidationError(f"{target.name} already exists.", hint="Use --force to overwrite it.")
        app.engine.approve(f"Overwrite {target.name}", RiskLevel.DANGEROUS, policy_action="workflow:create")
    if not app.options.dry_run:
        atomic_write_text(target, text)
        report = validate_file(target, loader=app.workflow_loader, check_tools=False, base_dir=app.root)
    else:
        report = None

    def render() -> None:
        out.success(f"{'Would create' if app.options.dry_run else 'Created'} {target.relative_to(app.root)}")
        if app.options.verbose or app.options.dry_run:
            out.plain(text)

    out.emit(
        {"workflow": name, "path": str(target), "dry_run": app.options.dry_run, "valid": report.ok if report else None},
        render,
    )
    return 0

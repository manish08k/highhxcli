"""highhx init"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.project.detector import detect_project
from highhx.project.initializer import apply_init, plan_init
from highhx.workflows.templates import TemplateLibrary


@click.command("init", short_help="Detect the project and create .highhx/ configuration.")
@click.option(
    "--stack",
    metavar="NAME",
    help="Template to use (python, node, react, nextjs, flutter, java, cpp, docker, monorepo, generic).",
)
@pass_app
def init(app: App, stack: str | None) -> int:
    """Inspect the project (manifests, lockfiles, Dockerfiles, .git …) and create
    .highhx/ with config, environment, policies and default workflows.

    Existing files are never overwritten unless you pass --force and confirm.
    """
    out = app.output
    root = app.start_dir if not app.initialized else app.root
    profile = detect_project(root)
    library = TemplateLibrary(app.plugins.template_dirs)
    if stack and stack not in library.stacks():
        raise click.BadParameter(
            f"unknown stack '{stack}' (available: {', '.join(library.stacks())})", param_hint="--stack"
        )
    plan = plan_init(profile, force=app.options.force, stack=stack, library=library)
    apply_init(app.engine, plan)
    data = {"ok": True, "dry_run": app.options.dry_run, "profile": profile.to_dict(), "plan": plan.to_dict()}

    def render() -> None:
        out.heading(f"HighhX init — {profile.name}")
        out.kv(
            {
                "root": str(profile.root),
                "stack": ", ".join(profile.stacks) or "unknown",
                "frameworks": ", ".join(d.name for d in profile.frameworks) or "-",
                "package managers": ", ".join(d.name for d in profile.package_managers) or "-",
                "databases": ", ".join(d.name for d in profile.databases) or "-",
                "containers": ", ".join(d.name for d in profile.containers) or "-",
                "monorepo": profile.monorepo.tool if profile.monorepo else "no",
                "git": "yes" if profile.git else "no",
                "template": plan.stack,
            }
        )
        for problem in profile.problems:
            out.warn(problem)
        if profile.commands:
            out.table(["command", "detected"], sorted(profile.commands.items()), title="Detected commands")
        verb = "Would create" if app.options.dry_run else "Created"
        for rel in plan.create:
            out.success(f"{verb} {rel}")
        for rel in plan.overwrite:
            out.success(f"{'Would overwrite' if app.options.dry_run else 'Overwrote'} {rel}")
        for rel in plan.skip:
            out.note(f"kept existing {rel} (use --force to overwrite)")
        if plan.gitignore:
            out.info(f"{'Would add' if app.options.dry_run else 'Added'} {', '.join(plan.gitignore)} to .gitignore")
        out.plain("")
        out.plain("Next: highhx doctor · highhx status · highhx workflow list")

    out.emit(data, render)
    return 0

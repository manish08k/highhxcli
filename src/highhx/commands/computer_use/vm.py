"""highhx vm — virtual machines (Lima, Tart) through the executor."""

from __future__ import annotations

from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup


def _act(app: App, name: str, inputs: dict[str, Any]) -> int:
    from highhx.actions.catalog import catalog_for
    from highhx.actions.executor import ActionExecutor
    from highhx.commands.computer.main import computer_session

    session = computer_session(app)
    executor = ActionExecutor(
        app, session.gate, actor=session.actor, catalog=catalog_for(app), computer=lambda: session
    )
    try:
        result = executor.run(name, inputs)
    finally:
        executor.close()
        session.close()

    def render() -> None:
        if not result.ok:
            app.output.error(result.error or result.summary)
        elif "vms" in result.output:
            rows = result.output["vms"]
            if not rows:
                app.output.info(result.summary)
            for row in rows:
                app.output.plain(f"  {row['name']:<20} {row['status']:<10} {row['backend']}")
        elif "stdout" in result.output:
            click.echo(result.output["stdout"], nl=False)
        else:
            app.output.success(result.summary)

    app.output.emit({"ok": result.ok, "status": result.status, **result.output, "error": result.error}, render)
    return 0 if result.ok else 1


@click.group(
    "vm",
    cls=DefaultGroup,
    default_command="list",
    short_help="Virtual machines (Lima, Tart): create, snapshot, run commands.",
)
def vm() -> None:
    """Whole isolated computers: create, start, pause, resume, stop, snapshot, restore, destroy,
    and run commands inside — through Lima (Linux VMs) or Tart (macOS/Linux VMs on Apple silicon).
    Every operation is an action: classified, approved, audited. A VM's desktop is operated by
    HighhX inside it, as an ssh:// computer."""


@vm.command("list", short_help="VMs and their state.")
@pass_app
def vm_list(app: App) -> int:
    """List the virtual machines of the installed backend."""
    return _act(app, "vm.list", {})


@vm.command("create", short_help="Create a VM.")
@click.argument("name")
@click.option("--image", default="", help="Lima template (default template://default) or Tart image to clone.")
@click.option("--cpus", type=int, default=2, show_default=True)
@click.option("--memory", "memory_mb", type=int, default=4096, show_default=True, help="MB")
@click.option("--disk", "disk_gb", type=int, default=30, show_default=True, help="GB")
@pass_app
def vm_create(app: App, name: str, image: str, cpus: int, memory_mb: int, disk_gb: int) -> int:
    """Create VM NAME with its own disk, network and limits."""
    return _act(
        app,
        "vm.create",
        {"name": name, **({"image": image} if image else {}), "cpus": cpus, "memory_mb": memory_mb, "disk_gb": disk_gb},
    )


def _simple(op: str, doc: str) -> Any:
    @vm.command(op, short_help=doc, help=doc)
    @click.argument("name")
    @pass_app
    def command(app: App, name: str) -> int:
        return _act(app, f"vm.{op}", {"name": name})

    return command


vm_start = _simple("start", "Start a VM.")
vm_stop = _simple("stop", "Stop a VM (its disk is kept).")
vm_pause = _simple("pause", "Suspend a VM (Tart).")
vm_resume = _simple("resume", "Resume a suspended VM.")
vm_destroy = _simple("destroy", "Delete a VM and its disk (asked).")


@vm.command("snapshot", short_help="Snapshot a VM.")
@click.argument("name")
@click.argument("tag")
@pass_app
def vm_snapshot(app: App, name: str, tag: str) -> int:
    """Snapshot VM NAME under TAG."""
    return _act(app, "vm.snapshot", {"name": name, "tag": tag})


@vm.command("restore", short_help="Restore a VM from a snapshot (asked).")
@click.argument("name")
@click.argument("tag")
@pass_app
def vm_restore(app: App, name: str, tag: str) -> int:
    """Restore VM NAME to snapshot TAG; its current state is lost."""
    return _act(app, "vm.restore", {"name": name, "tag": tag})


@vm.command("exec", short_help="Run a command inside a VM.", context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("argv", nargs=-1, type=click.UNPROCESSED, required=True)
@pass_app
def vm_exec(app: App, name: str, argv: tuple[str, ...]) -> int:
    """Run ARGV inside running VM NAME (classified like any command): highhx vm exec dev -- uname -a"""
    return _act(app, "vm.exec", {"name": name, "argv": list(argv)})

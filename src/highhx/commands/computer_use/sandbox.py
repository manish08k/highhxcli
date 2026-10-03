"""highhx sandbox — isolated workspaces for agent work."""

from __future__ import annotations

from typing import Any

import click

from highhx.commands import App, pass_app
from highhx.commands.computer.main import run_action
from highhx.commands.groups import DefaultGroup


@click.group("sandbox", cls=DefaultGroup, default_command="list", short_help="Isolated workspaces for agent work.")
def sandbox() -> None:
    """A copy of the project (secrets left out) with filesystem, network, environment and
    resource isolation — macOS Seatbelt, Linux bubblewrap or Docker. Run commands inside, look
    at the patch, apply it to the project (an approved change), destroy it."""


@sandbox.command("create", short_help="Create a sandbox from this project.")
@click.option("--isolation", type=click.Choice(["seatbelt", "bubblewrap", "docker", "workspace"]), help="Default: the strongest available.")
@click.option("--network", type=click.Choice(["deny", "allow"]), default="deny", show_default=True)
@click.option("--timeout", type=float, help="Seconds per command.")
@click.option("--memory", "memory_mb", type=int, help="Memory limit in MB (Linux and Docker).")
@click.option("--empty", is_flag=True, help="Start with an empty workspace instead of a copy.")
@pass_app
def sandbox_create(app: App, isolation: str | None, network: str, timeout: float | None, memory_mb: int | None, empty: bool) -> int:
    """Create a sandbox. `workspace` isolation is only a copy (no confinement) and asks first."""
    inputs: dict[str, Any] = {"network": network}
    for key, value in (("isolation", isolation), ("timeout", timeout), ("memory_mb", memory_mb)):
        if value is not None:
            inputs[key] = value
    if empty:
        inputs["copy"] = False

    def render(output: dict[str, Any]) -> None:
        app.output.kv({"id": output["id"], "isolation": output["isolation"], "network": output["network"], "workspace": output["workspace"]}, title="Sandbox")

    return run_action(app, "sandbox.create", inputs, render)


@sandbox.command("list", short_help="Sandboxes.")
@pass_app
def sandbox_list(app: App) -> int:
    """Every sandbox: id, isolation, network, commands run."""

    def render(output: dict[str, Any]) -> None:
        rows = [(s["id"], s["isolation"], s["network"], s.get("execs", 0), s["workspace"]) for s in output["sandboxes"]]
        app.output.table(["id", "isolation", "network", "runs", "workspace"], rows)

    return run_action(app, "sandbox.list", {}, render)


@sandbox.command("exec", short_help="Run a command inside a sandbox.", context_settings={"ignore_unknown_options": True})
@click.argument("sandbox_id")
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.option("--timeout", type=float)
@pass_app
def sandbox_exec(app: App, sandbox_id: str, command: tuple[str, ...], timeout: float | None) -> int:
    """`highhx sandbox exec sbx_… -- pytest -q` (classified like any command)."""

    def render(output: dict[str, Any]) -> None:
        for line in (output.get("stdout") or "").splitlines()[-40:]:
            app.output.plain(line)
        for line in (output.get("stderr") or "").splitlines()[-20:]:
            app.output.note(line)

    return run_action(app, "sandbox.exec", {"id": sandbox_id, "argv": list(command), **({"timeout": timeout} if timeout else {})}, render)


@sandbox.command("patch", short_help="The changes made inside, as a diff.")
@click.argument("sandbox_id")
@pass_app
def sandbox_patch(app: App, sandbox_id: str) -> int:
    """Print the unified diff of everything changed in the sandbox."""
    return run_action(app, "sandbox.patch", {"id": sandbox_id}, lambda output: app.output.plain(output["patch"] or "(no changes)"))


@sandbox.command("apply", short_help="Apply the sandbox's changes to the project.")
@click.argument("sandbox_id")
@pass_app
def sandbox_apply(app: App, sandbox_id: str) -> int:
    """Apply the patch to the project (shown and asked first)."""
    return run_action(app, "sandbox.apply", {"id": sandbox_id})


@sandbox.command("destroy", short_help="Kill everything in a sandbox and delete it.")
@click.argument("sandbox_id", required=False)
@click.option("--all", "all_", is_flag=True, help="Every sandbox (the kill switch).")
@pass_app
def sandbox_destroy(app: App, sandbox_id: str | None, all_: bool) -> int:
    """Destroy SANDBOX_ID, or --all."""
    return run_action(app, "sandbox.destroy", {"all": True} if all_ else {"id": sandbox_id or ""})

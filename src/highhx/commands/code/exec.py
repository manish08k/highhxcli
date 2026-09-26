"""highhx exec <command>"""

from __future__ import annotations

import click

from highhx.commands import App, exit_code_for, pass_app
from highhx.execution.command import CommandSpec
from highhx.execution.retry import RetryPolicy
from highhx.utils.time import parse_duration


@click.command(
    "exec",
    short_help="Run a command safely (risk check, env, history).",
    context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False},
)
@click.option("--timeout", metavar="DURATION", help="Kill the command after e.g. 30s, 5m.")
@click.option("--retry", "retries", type=click.IntRange(1, 100), default=1, show_default=True, help="Total attempts.")
@click.option(
    "--retry-delay",
    metavar="DURATION",
    default="1s",
    show_default=True,
    help="Initial delay between attempts (doubles each time).",
)
@click.option("--shell", "use_shell", is_flag=True, help="Run through the system shell.")
@click.option(
    "--interactive", "-i", is_flag=True, help="Attach the terminal (for prompts / TUIs); output is not captured."
)
@click.argument("command", nargs=-1, required=True, type=click.UNPROCESSED)
@pass_app
def exec_command(
    app: App,
    timeout: str | None,
    retries: int,
    retry_delay: str,
    use_shell: bool,
    interactive: bool,
    command: tuple[str, ...],
) -> int:
    """Run COMMAND in the project with the active environment profile injected.

    The command is classified by risk (e.g. `git push` is dangerous, `rm -rf`
    needs approval), recorded in history and its output logged (secrets redacted).
    """
    try:
        seconds = parse_duration(timeout)
        delay = parse_duration(retry_delay) or 0.0
    except ValueError as exc:
        raise click.BadParameter(str(exc)) from exc
    target: str | list[str] = command[0] if len(command) == 1 else list(command)
    spec = CommandSpec(
        target,
        cwd=app.start_dir,
        timeout=seconds,
        retry=RetryPolicy(attempts=retries, delay=delay),
        shell=True if use_shell else None,
        interactive=interactive,
        name=command[0].split()[0] if command else "exec",
    )
    result = app.engine.run(spec)
    data = result.to_dict(include_output=True)
    app.output.emit(
        data,
        (
            lambda: app.output.note(
                f"exit code {result.exit_code} · {result.duration:.2f}s"
                + (f" · {result.attempts} attempts" if result.attempts > 1 else "")
            )
        )
        if app.options.verbose
        else None,
    )
    if not result.ok and not result.dry_run and result.error and not app.options.json:
        app.output.error(result.error)
    return exit_code_for(result)

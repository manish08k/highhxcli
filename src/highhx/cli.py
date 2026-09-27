"""HighhX command-line entry point."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import click

from highhx import __version__
from highhx.commands import App
from highhx.core.errors import ExitCode, HighhXError
from highhx.core.lifecycle import Lifecycle

SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Interactive session & AI agent (HighhX Pro)", ("agent", "voice")),
    ("Automation (no AI)", ("do", "computer")),
    ("Account", ("login", "logout", "account")),
    ("Project", ("init", "status", "info", "dev", "start", "stop", "restart", "check")),
    ("Code & tasks", ("run", "exec", "script", "task", "watch", "fix")),
    ("Dependencies", ("deps",)),
    ("Testing & build", ("test", "benchmark", "build", "clean", "package", "artifacts")),
    ("Environment", ("env",)),
    ("Git & releases", ("git", "version", "changelog", "release", "publish")),
    ("Deployment", ("deploy", "rollback", "environments")),
    ("Security", ("security",)),
    ("Containers, services & data", ("docker", "services", "ports", "db")),
    ("Workflows & automation", ("workflow", "actions", "schedule", "hook", "trigger", "watchers")),
    ("Observability", ("logs", "history", "events", "audit", "report", "trace", "runs")),
    ("Extensibility & team", ("plugin", "config", "policy", "workspace", "profile")),
    ("Diagnostics", ("doctor", "diagnose", "repair", "debug")),
)

GLOBAL_EPILOG = (
    "Global options work with every command: --json, --dry-run, --yes/-y, --force, "
    "--quiet/-q, --verbose/-v, --debug, --no-color, --cwd/-C DIR, --config-profile NAME."
)


def _set_option(name: str) -> Callable[[click.Context, click.Parameter, Any], None]:
    def callback(ctx: click.Context, _param: click.Parameter, value: Any) -> None:
        if value in (None, False):
            return
        app = ctx.find_object(App)
        if app is None:
            return
        if name == "cwd":
            app.set_cwd(Path(value))
        else:
            setattr(app.options, name, value)

    return callback


def global_options(hidden: bool) -> list[click.Option]:
    def flag(decls: list[str], name: str, help_text: str) -> click.Option:
        return click.Option(
            decls,
            is_flag=True,
            default=False,
            expose_value=False,
            is_eager=True,
            hidden=hidden,
            help=help_text,
            callback=_set_option(name),
        )

    return [
        flag(["--json"], "json", "Machine-readable JSON output (no decoration)."),
        flag(["--dry-run"], "dry_run", "Show what would happen without changing anything."),
        flag(["--yes", "-y"], "yes", "Approve prompts (never bypasses non-bypassable policies)."),
        flag(["--force"], "force", "Allow overwriting existing files where a command supports it."),
        flag(["--quiet", "-q"], "quiet", "Only print errors."),
        flag(["--verbose", "-v"], "verbose", "Show more detail (commands being run …)."),
        flag(["--debug"], "debug", "Show tracebacks and debug logging."),
        flag(["--no-color"], "no_color", "Disable colors (also honours NO_COLOR)."),
        click.Option(
            ["--cwd", "-C"],
            metavar="DIR",
            expose_value=False,
            is_eager=True,
            hidden=hidden,
            help="Run as if started in DIR.",
            callback=_set_option("cwd"),
        ),
        click.Option(
            ["--config-profile"],
            metavar="NAME",
            expose_value=False,
            is_eager=True,
            hidden=hidden,
            help="Apply the config overlay .highhx/profiles/NAME.yaml (or set HIGHHX_PROFILE).",
            callback=_set_option("profile"),
        ),
    ]


def install_global_options(command: click.Command) -> None:
    existing = {opt for p in command.params for opt in getattr(p, "opts", [])}
    for option in global_options(hidden=True):
        if not set(option.opts) & existing:
            command.params.append(option)
    if command.epilog is None and not isinstance(command, click.Group):
        command.epilog = GLOBAL_EPILOG
    if isinstance(command, click.Group):
        for sub in command.commands.values():
            install_global_options(sub)


def is_request(args: list[str]) -> bool:
    """Arguments that read as a sentence: several words (quoted or not), not an option."""
    first = args[0]
    if first.startswith("-") or not first.strip():
        return False
    return len(args) > 1 or len(first.split()) > 1


class HighhXGroup(click.Group):
    """Root group: sectioned help and plugin-provided commands."""

    def _plugin_app(self, ctx: click.Context) -> App | None:
        return ctx.find_object(App)

    def list_commands(self, ctx: click.Context) -> list[str]:
        names = list(super().list_commands(ctx))
        app = self._plugin_app(ctx)
        if app is not None:
            try:
                names += [n for n in app.plugins.command_names() if n not in names]
            except HighhXError:
                pass
        return names

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        command = super().get_command(ctx, cmd_name)
        if command is not None:
            return command
        app = self._plugin_app(ctx)
        if app is None:
            return None
        from highhx.commands.plugins.main import plugin_click_command

        try:
            plugin_command = plugin_click_command(app, cmd_name)
        except HighhXError:
            return None
        if plugin_command is not None:
            install_global_options(plugin_command)
        return plugin_command

    def resolve_command(
        self, ctx: click.Context, args: list[str]
    ) -> tuple[str | None, click.Command | None, list[str]]:
        """``highhx "show git status"`` / ``highhx open gmail``: a plain-language request, not a
        command name — handled exactly like ``highhx agent "…"``. A single unknown word
        (``highhx stauts``) stays a usage error, and every real command wins."""
        if args and is_request(args) and self.get_command(ctx, args[0]) is None:
            agent = super().get_command(ctx, "agent")
            if agent is not None:
                return "agent", agent, ["run", *args]
        return super().resolve_command(ctx, args)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        placed: set[str] = set()
        for title, names in SECTIONS:
            rows = []
            for name in names:
                command = super().get_command(ctx, name)
                if command is None or command.hidden:
                    continue
                placed.add(name)
                rows.append((name, command.get_short_help_str(limit=70)))
            if rows:
                with formatter.section(title):
                    formatter.write_dl(rows)
        others = []
        for name in self.list_commands(ctx):
            if name in placed:
                continue
            command = self.get_command(ctx, name)
            if command is not None and not command.hidden:
                others.append((name, command.get_short_help_str(limit=70)))
        if others:
            with formatter.section("Plugins & other"):
                formatter.write_dl(others)


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"], "max_content_width": 110}


@click.group(
    cls=HighhXGroup,
    context_settings=CONTEXT_SETTINGS,
    invoke_without_command=True,
    epilog="Run `highhx <command> --help` for details, or `highhx doctor` to check your setup.",
)
@click.version_option(__version__, "--version", "-V", prog_name="highhx", message="%(prog)s %(version)s")
@click.pass_context
def cli(ctx: click.Context) -> int | None:
    """HighhX — your developer command center.

    Run `highhx` in a terminal for the interactive session, or pass a request
    directly: highhx "open Gmail and search internship", highhx "show git status".
    HighhX Free understands known requests without AI (deterministic resolver → JSON action plan →
    deterministic automation → verification) with no account; HighhX Pro adds
    the AI developer agent backed by the HighhX platform. Every risky action is
    classified (safe / normal / dangerous / critical) and needs approval; use
    --dry-run to preview anything.
    """
    if ctx.invoked_subcommand is not None:
        return None
    app = ctx.find_object(App)
    if app is not None:
        from highhx.agent.launch import interactive_terminal, start_interactive

        if interactive_terminal(app):
            return start_interactive(app)
    if app is not None and not app.options.json and app.output.console.is_terminal:
        _banner(app)
    click.echo(ctx.get_help())
    if app is not None and not app.options.json:
        _guide(app)
    return None


def _banner(app: App) -> None:
    """The HighhX knight with version and location (interactive terminals only)."""
    from highhx.ui.branding import banner, home_relative

    console = app.output.console
    console.print(
        banner(console, f"HighhX v{__version__}", "Developer command center", home_relative(str(app.start_dir)))
    )
    console.print()


def _guide(app: App) -> None:
    """Next-step guidance for a bare `highhx`: initialize the project, then try Pro."""
    out = app.output
    out.plain()
    if not app.initialized:
        out.info(f"{app.root.name} is not a HighhX project yet — run `highhx init` to set it up.")
    else:
        out.info("Try `highhx status`, `highhx check` or `highhx doctor`.")
    try:
        signed_in = app.cloud.signed_in
    except HighhXError:
        signed_in = False
    out.note("Run `highhx` in a terminal for the interactive session.")
    if not signed_in:
        out.note("HighhX Pro: `highhx login` adds the AI developer agent to that session.")


for option in global_options(hidden=False):
    cli.params.append(option)


def _register() -> None:
    from highhx.commands import registry

    for command in registry.all_commands():
        cli.add_command(command)
    for name, command in cli.commands.items():
        if name:
            install_global_options(command)


_register()


def _emit_error(app: App, error: HighhXError) -> None:
    out = app.output
    if app.options.json:
        if not out.json_emitted:
            out.json({"ok": False, **error.to_dict()})
        return
    from highhx.ui.errors import render_error

    render_error(out.err_console, error, out.symbols)


def main(argv: list[str] | None = None) -> None:
    """Console-script entry point."""
    sys.exit(run(argv))


def run(argv: list[str] | None = None, *, app: App | None = None) -> int:
    """Run the CLI and return the exit code (used by tests)."""
    app = app or App()
    lifecycle = Lifecycle(app.ctx.cancel)
    lifecycle.add_shutdown_hook(app.close)
    code: int = ExitCode.OK
    try:
        with lifecycle.handle_signals():
            result = cli.main(args=argv, prog_name="highhx", standalone_mode=False, obj=app)
        code = int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
        if app.options.json and not app.output.json_emitted:
            # Every command produces exactly one JSON document in --json mode.
            app.output.json({"ok": code == 0, "exit_code": code})
    except click.exceptions.Exit as exc:
        code = exc.exit_code
    except click.exceptions.Abort:
        code = ExitCode.CANCELLED
    except click.ClickException as exc:
        if app.options.json:
            app.output.json(
                {"ok": False, "error": "usage", "message": exc.format_message(), "exit_code": exc.exit_code}
            )
        else:
            exc.show()
        code = exc.exit_code
    except HighhXError as exc:
        _emit_error(app, exc)
        code = int(exc.exit_code)
    except KeyboardInterrupt:
        if not app.options.json:
            app.output.err_console.print("\n[warn]Interrupted.[/warn]")
        code = ExitCode.CANCELLED
    except Exception as exc:
        if app.options.json:
            app.output.json(
                {"ok": False, "error": "unexpected", "message": f"{type(exc).__name__}: {exc}", "exit_code": 1}
            )
        else:
            from highhx.ui.errors import render_unexpected

            render_unexpected(app.output.err_console, exc, app.output.symbols, debug=app.options.debug)
        code = ExitCode.FAILURE
    finally:
        lifecycle.shutdown()
    return code


if __name__ == "__main__":  # pragma: no cover
    main()

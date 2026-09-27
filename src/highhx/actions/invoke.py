"""Run a HighhX command in-process, on the same application (history, approvals, output)."""

from __future__ import annotations

import contextlib
import dataclasses
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import click

from highhx.core.errors import ExitCode, HighhXError, OperationCancelledError

if TYPE_CHECKING:
    from highhx.commands import App


@contextlib.contextmanager
def _nested_output(app: App) -> Iterator[None]:
    """In --json mode the caller emits the only JSON document: a nested command prints its human
    output to stderr instead of a second document on stdout."""
    if not app.options.json:
        yield
        return
    import sys

    from highhx.ui.output import Output

    outer = app.output
    nested = Output(quiet=outer.quiet, stdout=sys.stderr, stderr=sys.stderr, redactor=app.redactor)
    engine = app.__dict__.get("engine")
    app.__dict__["output"] = nested
    app.options.json = False
    if engine is not None and engine.output is outer:
        engine.output = nested
    try:
        yield
    finally:
        app.__dict__["output"] = outer
        app.options.json = True
        if engine is not None and engine.output is nested:
            engine.output = outer


def invoke_cli(app: App, argv: list[str]) -> int:
    """``highhx <argv>`` inside this process; returns its exit code. Flags given to this one
    command (``--json`` …) do not stick to the application afterwards."""
    from highhx.cli import _emit_error, cli

    options = dataclasses.replace(app.options)
    try:
        with _nested_output(app):
            return _invoke(app, argv, cli, _emit_error)
    finally:
        for item in dataclasses.fields(options):
            setattr(app.options, item.name, getattr(options, item.name))


def _invoke(app: App, argv: list[str], cli: Any, _emit_error: Any) -> int:
    try:
        result = cli.main(args=list(argv), prog_name="highhx", standalone_mode=False, obj=app)
        return int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
    except click.exceptions.Exit as exc:
        return int(exc.exit_code)
    except click.exceptions.Abort:
        return int(ExitCode.CANCELLED)
    except click.ClickException as exc:
        exc.show()
        return int(exc.exit_code)
    except (KeyboardInterrupt, OperationCancelledError):
        raise
    except HighhXError as exc:
        _emit_error(app, exc)
        return int(exc.exit_code)

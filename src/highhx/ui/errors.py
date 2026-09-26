"""Rendering of errors for humans."""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

from highhx.core.errors import HighhXError
from highhx.ui.terminal import Symbols


def render_error(console: Console, error: HighhXError, symbols: Symbols) -> None:
    """Print a HighhX error with details and an actionable hint."""
    console.print(f"[fail]{symbols.fail} {escape(error.message)}[/fail]")
    if error.details:
        console.print()
        for detail in error.details[:50]:
            console.print(f"  {symbols.bullet} {escape(detail)}")
        if len(error.details) > 50:
            console.print(f"  … and {len(error.details) - 50} more")
    if error.hint:
        console.print()
        console.print(f"[info]{escape(error.hint)}[/info]")


def render_unexpected(console: Console, exc: BaseException, symbols: Symbols, *, debug: bool) -> None:
    console.print(f"[fail]{symbols.fail} Unexpected error: {escape(type(exc).__name__)}: {escape(str(exc))}[/fail]")
    if debug:
        console.print_exception(show_locals=False)
    else:
        console.print("[muted]Re-run with --debug for a full traceback, and please report this as a bug.[/muted]")

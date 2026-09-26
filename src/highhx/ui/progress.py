"""Spinners and progress indicators that disappear in JSON/quiet/non-TTY mode."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from rich.console import Console


@contextlib.contextmanager
def spinner(console: Console, message: str, *, enabled: bool = True) -> Iterator[None]:
    """Show a spinner while the block runs (only on an interactive terminal)."""
    if not enabled or not console.is_terminal:
        yield
        return
    with console.status(message, spinner="dots"):
        yield

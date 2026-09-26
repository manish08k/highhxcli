"""Console construction and status symbols."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import TextIO

from rich.console import Console
from rich.theme import Theme

THEME = Theme(
    {
        "ok": "green",
        "warn": "yellow",
        "fail": "bold red",
        "info": "cyan",
        "muted": "dim",
        "title": "bold",
        "risk.safe": "green",
        "risk.normal": "cyan",
        "risk.dangerous": "yellow",
        "risk.critical": "bold red",
    }
)


@dataclass(frozen=True)
class Symbols:
    ok: str
    warn: str
    fail: str
    info: str
    skip: str
    arrow: str
    bullet: str


UNICODE_SYMBOLS = Symbols("✓", "⚠", "✗", "•", "○", "→", "•")
ASCII_SYMBOLS = Symbols("OK", "!", "X", "-", "o", "->", "*")


def _supports_unicode(stream: TextIO) -> bool:
    encoding = (getattr(stream, "encoding", None) or "").lower()
    return "utf" in encoding


def symbols_for(stream: TextIO) -> Symbols:
    if os.environ.get("HIGHHX_ASCII"):
        return ASCII_SYMBOLS
    return UNICODE_SYMBOLS if _supports_unicode(stream) else ASCII_SYMBOLS


def color_disabled(no_color: bool) -> bool:
    """Respect --no-color and the NO_COLOR convention (https://no-color.org)."""
    return no_color or bool(os.environ.get("NO_COLOR"))


def create_console(*, stderr: bool = False, no_color: bool = False, file: TextIO | None = None) -> Console:
    stream = file or (sys.stderr if stderr else sys.stdout)
    return Console(
        file=stream,
        theme=THEME,
        no_color=color_disabled(no_color),
        highlight=False,
        soft_wrap=False,
        emoji=False,
    )

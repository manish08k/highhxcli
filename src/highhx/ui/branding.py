"""The HighhX knight: terminal-native brand art.

The knight is drawn with text only — Unicode quadrant blocks (primary) and plain ASCII
(fallback) — hand-built from the HighhX knight reference design. No image files, image
protocols or dependencies are involved; the art is just these strings.

Sizes: the full knight is 26 x 31 cells, the compact one 13 x 15. :func:`banner_lines`
chooses one for the terminal (or none when it is too narrow) and places the title text
beside it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.text import Text

# fmt: off
KNIGHT_UNICODE = """\
            ▟▙
           ▟██▙
           ████
           ▐▛▜▌
           ▐▌▐▌
            ▘▝
          ▟▗▟▙▖▙
         ▟█▝██▘█▙
        ▟▀▗▖██▗▖▀▙
       ▟▚▟█▌██▐█▙▞▙
      ▝▚███▌██▐███▞▘
      ▗████▌██▐████▖
      ▞█████▐▌█████▚
      ▌█████▐▌█████▐
      ▌█████▐▌█████▐
    ▗▜▌▞▀███▐▌███▀▚▐▛▖
    ▐▐▌██▄▞▀▐▌▀▚▄██▐▌▌
    ▐▐▌█▄▀▜█▄▄█▛▀▄█▐▌▌
    ▝▟▌███▐█▀▀█▌███▐▙▘
      ▌▛██▐█▐▌█▌██▜▐
   ▗  ▌▌▛█▐█▐▌█▌█▜▐▐  ▖
  ▗█▙  ▌▌█▐█▐▌█▌█▐▐  ▟█▖
  ▝██▙▖▜▙█▐█▐▌█▌█▟▛▗▟██▘
  ▄▜███▖▜█▐█▐▌█▌█▛▗███▛▄
▜██▙▀██▖ █▝█▐▌█▘█ ▗██▀▟██▛
 ▀██▙▖▜▙ ▝ █▐▌█ ▘ ▟▛▗▟██▀
  ▝▜██▙▞▘  █▐▌█  ▝▚▟██▛▘
    ▝▀▜█▙  ▜▐▌▛  ▟█▛▀▘
       ▝▜▌  ▐▌  ▐▛▘
         ▜  ▐▌  ▛
          ▘ ▝▘ ▝
"""

KNIGHT_UNICODE_COMPACT = """\
     ▗█▖
     ▐▀▌
    ▗ ▄ ▖
   ▗▚▐█▌▞▖
  ▗▚█▐█▌█▞▖
   ▟█▐█▌█▙
  ▐▐█▌█▐█▌▌
  █▗▜▌█▐▛▖█
  █▐▙▘█▝▟▌█
  ▐▐▜▌▄▐▛▌▌
 ▗▝▐▐▌█▐▌▌▘▖
 █▙▝█▌█▐█▘▟█
▜▙▜█▖▘█▝▗█▛▟▛
 ▀█▟▀ █ ▀▙█▀
   ▀▌ █ ▐▀
"""

KNIGHT_ASCII = """\
            ##
           ####
           ####
           |##|
           ||||
            ''
          #.##.#
         ##'##'##
        #"..##.."#
       #\\##|##|##/#
      '\\###|##|###/'
      .####|##|####.
      /#####||#####\\
      |#####||#####|
      |#####||#####|
    .#|/"###||###"\\|#.
    |||##_/"||"\\_##|||
    |||#_"##__##"_#|||
    '#|###|#""#|###|#'
      |###|#||#|###|
   .  ||##|#||#|##||  .
  .##  ||#|#||#|#||  ##.
  '###.###|#||#|###.###'
  _####.##|#||#|##.####_
####"##. #'#||#'# .##"####
 "###.## ' #||# ' ##.###"
  '####/'  #||#  '\\####'
    '"###  #||#  ###"'
       '#|  ||  |#'
         #  ||  #
          ' '' '
"""

KNIGHT_ASCII_COMPACT = """\
     .#.
     |"|
    . _ .
   .\\|#|/.
  .\\#|#|#/.
   ##|#|##
  ||#|#|#||
  #.#|#|#.#
  #|#'#'#|#
  ||#|_|#||
 .'|||#|||'.
 ##'#|#|#'##
####.'#'.####
 "##" # "##"
   "| # |"
"""

# fmt: on

GAP = 4
"""Columns between the knight and the text beside it."""
MIN_TEXT_WIDTH = 20
"""Narrowest text column worth showing next to the knight."""
FULL_MIN_HEIGHT = 44
"""Terminal rows needed before the full-size knight is used (it is 31 rows tall)."""


@dataclass(frozen=True)
class Art:
    name: str
    lines: tuple[str, ...]

    @property
    def width(self) -> int:
        return max(len(line) for line in self.lines)

    @property
    def height(self) -> int:
        return len(self.lines)

    def padded(self) -> list[str]:
        """Every line padded to the art's width (the stored strings are right-stripped)."""
        return [line.ljust(self.width) for line in self.lines]


def _art(name: str, text: str) -> Art:
    return Art(name, tuple(text.rstrip("\n").split("\n")))


ARTS = {
    ("unicode", "full"): _art("unicode-full", KNIGHT_UNICODE),
    ("unicode", "compact"): _art("unicode-compact", KNIGHT_UNICODE_COMPACT),
    ("ascii", "full"): _art("ascii-full", KNIGHT_ASCII),
    ("ascii", "compact"): _art("ascii-compact", KNIGHT_ASCII_COMPACT),
}


def knight(*, unicode: bool = True, compact: bool = False) -> Art:
    return ARTS[("unicode" if unicode else "ascii", "compact" if compact else "full")]


def supports_unicode(console: Console) -> bool:
    """Block characters are safe when the output encoding is UTF-* and ASCII was not requested."""
    if os.environ.get("HIGHHX_ASCII"):
        return False
    return "utf" in (console.encoding or "").lower()


@dataclass(frozen=True)
class Layout:
    art: Art | None
    stacked: bool = False
    """True: the knight above the text (terminal too narrow to put them side by side)."""


def choose(width: int, height: int | None, *, unicode: bool, preference: str | None = None) -> Layout:
    """How to draw the banner in a ``width`` x ``height`` terminal.

    Side by side when the knight and at least ``MIN_TEXT_WIDTH`` columns of text fit; the
    full knight only in tall terminals; otherwise the compact knight above the text; text
    only when even that does not fit. ``preference`` (``HIGHHX_BANNER``): ``full`` /
    ``compact`` force a size where it fits, ``off`` hides the knight.
    """
    preference = (preference or "").strip().lower()
    if preference in ("off", "none", "0", "false", "no"):
        return Layout(None)
    full, compact = knight(unicode=unicode), knight(unicode=unicode, compact=True)
    beside = [a for a in (full, compact) if a.width + GAP + MIN_TEXT_WIDTH <= width]
    tall = height is not None and height >= FULL_MIN_HEIGHT
    if full in beside and (preference == "full" or (tall and preference != "compact")):
        return Layout(full)
    if compact in beside:
        return Layout(compact)
    if compact.width <= width:
        return Layout(compact, stacked=True)
    return Layout(None)


def _text_row(art: Art, count: int) -> int:
    """Row of the knight where the text block starts: beside the upper helmet."""
    return max(0, (art.height - count) // 2 - art.height // 6)


def fit(text: str, width: int, *, keep_end: bool = False) -> str:
    """``text`` cut to ``width`` columns with an ellipsis (from the left for paths)."""
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return "…" + text[-(width - 1) :] if keep_end else text[: width - 1] + "…"


def _rows(
    layout: Layout, entries: Sequence[tuple[str, str]], width: int, *, location: int | None = None
) -> list[list[tuple[str, str]]]:
    """Styled segments per output row (shared by the plain and the Rich renderers).
    Entry ``location`` (default: the last of several) is a path: it keeps its end when shortened."""
    if location is None and len(entries) > 2:
        location = len(entries) - 1

    def cut(index: int, line: str, room: int) -> str:
        return fit(line, room, keep_end=index == location)

    art, accent = layout.art, entries[0][1] if entries else ""
    if art is None:
        return [[(cut(i, line, width), style)] for i, (line, style) in enumerate(entries)]
    if layout.stacked:
        figure = [[(row.rstrip(), accent)] for row in art.padded()]
        return [*figure, [], *([(cut(i, line, width), style)] for i, (line, style) in enumerate(entries))]
    text_width = width - art.width - GAP
    top = _text_row(art, len(entries))
    rows = []
    for index, row in enumerate(art.padded()):
        if 0 <= index - top < len(entries):
            line, style = entries[index - top]
            rows.append([(row, accent), (" " * GAP, ""), (cut(index - top, line, text_width), style)])
        else:
            rows.append([(row.rstrip(), accent)])
    return rows


def banner_lines(layout: Layout, text: Sequence[str], *, width: int) -> list[str]:
    """The banner as plain lines (no styles)."""
    return ["".join(part for part, _ in row) for row in _rows(layout, [(t, "") for t in text], width)]


def banner(
    console: Console,
    title: str,
    subtitle: str,
    detail: str = "",
    *,
    accent: str = "bold",
    status: Sequence[tuple[str, str]] = (),
) -> Text:
    """The startup banner for ``console``: the knight (when it fits) with title, subtitle, detail
    (a location) and optional ``status`` lines ``(text, style)`` below them."""
    layout = choose(
        console.width, console.height, unicode=supports_unicode(console), preference=os.environ.get("HIGHHX_BANNER")
    )
    entries = [(line, style) for line, style in ((title, accent), (subtitle, ""), (detail, "dim")) if line]
    location = len(entries) - 1 if detail and len(entries) > 2 else None
    entries += [(line, style) for line, style in status if line]
    out = Text()
    for index, row in enumerate(_rows(layout, entries, console.width, location=location)):
        if index:
            out.append("\n")
        for part, style in row:
            out.append(part, style=style)
    return out


def home_relative(path: str) -> str:
    """``/Users/me/project`` -> ``~/project`` for display."""
    home = str(Path.home())
    return "~" + path[len(home) :] if (home and path.startswith(home + os.sep)) or path == home else path

"""Table helpers."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from rich import box
from rich.table import Table


def make_table(
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    title: str | None = None,
    show_header: bool = True,
) -> Table:
    """Build a compact table with the house style."""
    table = Table(title=title, box=box.SIMPLE_HEAD, show_header=show_header, title_justify="left", pad_edge=False)
    for index, column in enumerate(columns):
        table.add_column(column, overflow="fold", no_wrap=index == 0)
    for row in rows:
        table.add_row(*("" if cell is None else str(cell) for cell in row))
    return table


def key_value_table(data: dict[str, Any], *, title: str | None = None) -> Table:
    table = Table(title=title, box=None, show_header=False, title_justify="left", pad_edge=False)
    table.add_column("key", style="muted", no_wrap=True)
    table.add_column("value", overflow="fold")
    for key, value in data.items():
        if isinstance(value, list | tuple):
            value = ", ".join(str(v) for v in value) or "-"
        table.add_row(str(key), "-" if value in (None, "") else str(value))
    return table

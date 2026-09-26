"""Status dashboard rendering."""

from __future__ import annotations

from typing import Any

from rich.columns import Columns
from rich.console import Group
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from highhx.ui.output import Output
from highhx.utils.time import format_duration, humanize_ago


def _panel(title: str, rows: list[tuple[str, str]]) -> Panel:
    table = Table(box=None, show_header=False, pad_edge=False)
    table.add_column(style="muted", no_wrap=True)
    table.add_column(overflow="fold")
    for key, value in rows:
        table.add_row(key, value)
    return Panel(table, title=f"[title]{escape(title)}[/title]", title_align="left", border_style="muted")


def render_status(out: Output, status: dict[str, Any]) -> None:
    """Render the ``highhx status`` dashboard from its JSON-able data."""
    project = status.get("project") or {}
    git = status.get("git") or {}
    env = status.get("environment") or {}
    panels: list[Panel] = []

    panels.append(
        _panel(
            "Project",
            [
                ("name", escape(str(project.get("name") or "-"))),
                ("root", escape(str(project.get("root") or "-"))),
                ("stack", escape(", ".join(project.get("stacks") or []) or "-")),
                ("initialized", "yes" if project.get("initialized") else "[warn]no — run highhx init[/warn]"),
            ],
        )
    )
    if git.get("available"):
        state = "[ok]clean[/ok]" if git.get("clean") else f"[warn]{git.get('changes', 0)} change(s)[/warn]"
        panels.append(
            _panel(
                "Git",
                [
                    ("branch", escape(str(git.get("branch") or "-"))),
                    ("state", state),
                    ("ahead/behind", f"{git.get('ahead', 0)}/{git.get('behind', 0)}"),
                    ("last commit", escape(str(git.get("last_commit") or "-"))),
                ],
            )
        )
    else:
        panels.append(_panel("Git", [("repository", "[muted]not a git repository[/muted]")]))
    panels.append(
        _panel(
            "Environment",
            [
                ("profile", escape(str(env.get("profile") or "-"))),
                ("variables", str(env.get("variables", 0))),
                (
                    "missing",
                    "[ok]none[/ok]" if not env.get("missing") else f"[fail]{escape(', '.join(env['missing']))}[/fail]",
                ),
            ],
        )
    )

    services = status.get("services") or []
    service_rows = [
        (
            escape(s["name"]),
            f"{out.status_symbol('success' if s.get('running') else 'skip')} "
            f"{'running' if s.get('running') else 'stopped'}" + (f" :{s['port']}" if s.get("port") else ""),
        )
        for s in services
    ] or [("services", "[muted]none configured[/muted]")]
    panels.append(_panel("Services", service_rows))

    recent = status.get("recent") or []
    recent_rows = [
        (
            escape(r["name"]),
            f"{out.status_symbol(r['status'])} {escape(r['status'])} · {format_duration(r.get('duration'))}"
            f" · {humanize_ago(r.get('started_at'))}",
        )
        for r in recent[:5]
    ] or [("history", "[muted]no executions yet[/muted]")]
    panels.append(_panel("Recent runs", recent_rows))

    out.print(Group(Columns(panels, equal=False, expand=False)))

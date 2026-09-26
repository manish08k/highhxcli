"""Regenerate docs/commands.md from the CLI's own help text.

Usage: python scripts/generate_docs.py
"""

from __future__ import annotations

from pathlib import Path

import click

from highhx.cli import SECTIONS, cli

EXIT_CODES = (
    (0, "Success"),
    (1, "Failure (command, step or check failed)"),
    (2, "Usage error"),
    (3, "Configuration error / not initialized"),
    (4, "Not found (workflow, target, execution …)"),
    (5, "Required tool missing"),
    (6, "Approval denied"),
    (7, "Blocked by policy"),
    (8, "Validation failed"),
    (9, "Findings reported (security, audit)"),
    (124, "Timed out"),
    (127, "Command not found"),
    (130, "Cancelled (Ctrl+C)"),
)


def _usage(command: click.Command, path: str) -> str:
    ctx = click.Context(command, info_name=path)
    return f"highhx {path} {' '.join(command.collect_usage_pieces(ctx))}".strip()


def _document(lines: list[str], ctx: click.Context, command: click.Command, path: str, level: int) -> None:
    lines += [f"{'#' * level} `highhx {path}`", ""]
    lines += [(command.help or command.short_help or "").replace("\b\n", "").strip(), ""]
    if not isinstance(command, click.Group):
        lines += ["```", _usage(command, path), "```", ""]
    options = [p for p in command.params if isinstance(p, click.Option) and not p.hidden]
    if options:
        lines += ["| Option | Description |", "|---|---|"]
        for option in options:
            default = (
                f" (default: `{option.default}`)" if option.show_default and option.default not in (None, False) else ""
            )
            lines.append(f"| `{', '.join(option.opts)}` | {(option.help or '').replace('|', '/')}{default} |")
        lines.append("")
    if isinstance(command, click.Group):
        default_command = getattr(command, "default_command", None)
        if default_command:
            lines += [f"Running `highhx {path}` without a subcommand runs `highhx {path} {default_command}`.", ""]
        for name in command.list_commands(ctx):
            sub = command.get_command(ctx, name)
            if sub is not None and not sub.hidden:
                _document(lines, ctx, sub, f"{path} {name}", level + 1)


def render() -> str:
    ctx = click.Context(cli, info_name="highhx")
    lines = [
        "# Command reference",
        "",
        "Generated from the CLI's own help text by `scripts/generate_docs.py`. Every command",
        "also accepts the global options below, before or after the command name.",
        "",
        "## Global options",
        "",
        "| Option | Effect |",
        "|---|---|",
    ]
    for param in cli.params:
        if isinstance(param, click.Option) and not param.hidden and param.help:
            lines.append(f"| `{', '.join(param.opts + param.secondary_opts)}` | {param.help} |")
    lines += ["", "## Exit codes", "", "| Code | Meaning |", "|---|---|"]
    lines += [f"| {code} | {meaning} |" for code, meaning in EXIT_CODES]
    lines.append("")
    for title, names in SECTIONS:
        lines += [f"## {title}", ""]
        for name in names:
            _document(lines, ctx, cli.commands[name], name, 3)
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "docs" / "commands.md"
    target.write_text(render(), encoding="utf-8")
    print(f"wrote {target}")

"""Click helpers shared by command modules."""

from __future__ import annotations

from typing import Any

import click


class DefaultGroup(click.Group):
    """A group that runs ``default_command`` when no known subcommand is given.

    ``highhx deploy staging`` → ``highhx deploy to staging``;
    ``highhx git`` → ``highhx git status``.
    """

    def __init__(self, *args: Any, default_command: str, **kwargs: Any) -> None:
        kwargs.setdefault("invoke_without_command", False)
        super().__init__(*args, **kwargs)
        self.default_command = default_command

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if not args or (args[0] not in self.commands and args[0] not in ctx.help_option_names):
            args = [self.default_command, *args]
        return super().parse_args(ctx, args)

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        rows = []
        for name in self.list_commands(ctx):
            command = self.get_command(ctx, name)
            if command is None or command.hidden:
                continue
            label = f"{name} (default)" if name == self.default_command else name
            rows.append((label, command.get_short_help_str(limit=80)))
        if rows:
            with formatter.section("Commands"):
                formatter.write_dl(rows)

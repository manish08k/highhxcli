"""Every public command must be documented in docs/commands.md."""

from pathlib import Path

import click

from highhx.cli import cli

DOCS = (Path(__file__).resolve().parents[3] / "docs" / "commands.md").read_text()


def walk(command: click.Command, path: str) -> list[str]:
    paths = [path]
    if isinstance(command, click.Group):
        for name, sub in command.commands.items():
            if not sub.hidden:
                paths.extend(walk(sub, f"{path} {name}"))
    return paths


def test_every_command_is_documented() -> None:
    missing = [p for name, cmd in cli.commands.items() for p in walk(cmd, name) if f"`highhx {p}`" not in DOCS]
    assert missing == []


def test_every_command_has_help_text() -> None:
    def missing(command: click.Command, path: str) -> list[str]:
        found = [] if (command.help or "").strip() else [path]
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                found += missing(sub, f"{path} {name}")
        return found

    assert [p for name, cmd in cli.commands.items() for p in missing(cmd, name)] == []


def test_documented_commands_exist() -> None:
    import re

    documented = set(re.findall(r"^#+ `highhx ([a-z0-9 -]+)`", DOCS, re.MULTILINE))
    existing = {p for name, cmd in cli.commands.items() for p in walk(cmd, name)}
    assert documented - existing == set()

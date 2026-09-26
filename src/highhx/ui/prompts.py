"""User prompts (implements the approvals Prompter protocol)."""

from __future__ import annotations

import sys

from rich.console import Console
from rich.markup import escape


class ConsolePrompter:
    """Asks questions on the terminal. Never prompts when not interactive."""

    def __init__(self, console: Console, *, interactive: bool) -> None:
        self.console = console
        self._interactive = interactive

    @property
    def interactive(self) -> bool:
        return self._interactive

    def _ask(self, message: str) -> str:
        self.console.print(message, end="")
        try:
            return sys.stdin.readline().strip()
        except (EOFError, OSError):
            return ""

    def confirm(self, message: str, *, default: bool = False) -> bool:
        if not self._interactive:
            return default
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self._ask(f"[warn]?[/warn] {escape(message)}{suffix}").lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def confirm_typed(self, message: str, expected: str) -> bool:
        if not self._interactive:
            return False
        self.console.print(f"[fail]![/fail] {escape(message)}")
        answer = self._ask(f"  Type [bold]{escape(expected)}[/bold] to confirm: ")
        return answer == expected

    def ask(self, message: str, *, default: str | None = None, secret: bool = False) -> str | None:
        if not self._interactive:
            return default
        if secret:
            import getpass

            value = getpass.getpass(f"{message}: ")
            return value or default
        hint = f" [{default}]" if default else ""
        answer = self._ask(f"{escape(message)}{escape(hint)}: ")
        return answer or default

    def choose(self, message: str, choices: list[str], *, default: str | None = None) -> str | None:
        if not self._interactive or not choices:
            return default
        for index, choice in enumerate(choices, start=1):
            self.console.print(f"  {index}) {escape(choice)}")
        answer = self._ask(f"{escape(message)} [1-{len(choices)}]: ")
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer if answer in choices else default


class StaticPrompter:
    """Non-interactive prompter with predetermined answers (tests, automation)."""

    def __init__(self, *, interactive: bool = True, answer: bool = True, typed: str | None = None) -> None:
        self._interactive = interactive
        self.answer = answer
        self.typed = typed
        self.asked: list[str] = []

    @property
    def interactive(self) -> bool:
        return self._interactive

    def confirm(self, message: str, *, default: bool = False) -> bool:
        self.asked.append(message)
        return self.answer

    def confirm_typed(self, message: str, expected: str) -> bool:
        self.asked.append(message)
        return (self.typed if self.typed is not None else (expected if self.answer else "")) == expected

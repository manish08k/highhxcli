import sys

import pytest

from highhx.execution import command as command_module
from highhx.execution.command import CommandSpec, split_command
from highhx.execution.shell import needs_shell, shell_argv


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("pytest -q", False),
        ("ruff check .", False),
        ("echo hi | wc -l", True),
        ("make build && make test", True),
        ("echo $HOME", True),
        ("cat file > out.txt", True),
        ("echo '$NOT_EXPANDED'", False),
        ("ls *.py", True),
    ],
)
def test_needs_shell(text: str, expected: bool) -> None:
    assert needs_shell(text) is expected


def test_string_without_shell_syntax_is_split() -> None:
    assert CommandSpec("python -m pytest -k 'a and b'").argv()[-2:] == ["-k", "a and b"]


def test_shell_syntax_uses_platform_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    argv = CommandSpec("echo a && echo b").argv()
    assert argv[-1] == "echo a && echo b"
    monkeypatch.setattr("highhx.execution.shell.IS_WINDOWS", True)
    assert shell_argv("dir")[1:] == ["/d", "/s", "/c", "dir"]


def test_argv_sequence_never_uses_shell() -> None:
    spec = CommandSpec(["echo", "a && b"])
    assert not spec.uses_shell()
    assert spec.argv() == ["echo", "a && b"]


def test_empty_command_is_rejected() -> None:
    with pytest.raises(ValueError):
        CommandSpec("   ").argv()


def test_self_command_falls_back_to_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_module, "which", lambda _name: None)
    assert CommandSpec("highhx status").argv() == [sys.executable, "-m", "highhx", "status"]


def test_windows_splitting_keeps_backslashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(command_module, "IS_WINDOWS", True)
    assert split_command(r'C:\tools\app.exe "C:\my dir\file.txt"') == [r"C:\tools\app.exe", r"C:\my dir\file.txt"]

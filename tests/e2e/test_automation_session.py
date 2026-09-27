"""End to end in a real pseudo-terminal: the Free session on the action engine."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.agent.test_interactive_shell import _pty_session as _raw_session


def _pty_session(root: Path, keys: list[bytes], **size: int) -> str:
    import re

    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", _raw_session(root, keys, **size))


pytestmark = [pytest.mark.e2e, pytest.mark.skipif(sys.platform == "win32", reason="POSIX pseudo-terminal")]


def _repo(root: Path) -> None:
    (root / "README.md").write_text("# demo\n")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_real_terminal_actions_plan_approve_and_tools(tmp_path: Path) -> None:
    _repo(tmp_path)
    keys = [
        b"show git status\r",
        b"/plan read README.md\r",
        b"/approve\r",
        b"/tools git\r",
        b"/voice status\r",
        b"/history\r",
        b"\x04",
    ]
    text = _pty_session(tmp_path, keys, cols=110, rows=40)
    assert "Free • Local" in text
    assert "◉ git status  git.status · safe" in text and "✓ git status" in text
    assert "filesystem.read" in text and "/approve runs exactly this plan" in text
    assert "✓ filesystem.read" in text
    assert "git.push" in text and "high" in text
    assert "Speech-to-text" in text or "speech-to-text" in text.lower()
    assert "filesystem.read — README.md" in text  # /history: the session's timeline
    assert "Bye." in text


def test_real_terminal_blocks_catastrophic_commands(tmp_path: Path) -> None:
    _repo(tmp_path)
    text = _pty_session(tmp_path, [b"!rm -rf /\r", b"\x04"], cols=110, rows=40)
    assert "blocked by policy" in text and "Bye." in text

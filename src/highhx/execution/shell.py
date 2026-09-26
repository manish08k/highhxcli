"""Shell selection and shell-syntax detection.

HighhX runs commands *without* a shell whenever possible (safer and portable).
A shell is used only when the command string contains shell syntax such as
pipes, redirection, ``&&`` or variable expansion — or when explicitly requested.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from highhx.utils.platform import IS_WINDOWS

_SHELL_SYNTAX = re.compile(r"(\|\||&&|[|;<>`]|\$\(|\$\{?[A-Za-z_]|(^|\s)&(\s|$)|\*|\?|~/|%[A-Za-z_]+%)")


def needs_shell(command: str) -> bool:
    """Return True if ``command`` uses shell syntax that requires a shell to interpret."""
    # Ignore characters inside single quotes (POSIX literal strings).
    stripped = re.sub(r"'[^']*'", "''", command)
    return bool(_SHELL_SYNTAX.search(stripped))


def shell_argv(command: str) -> list[str]:
    """argv that runs ``command`` through the platform's default shell."""
    if IS_WINDOWS:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/s", "/c", command]
    sh = "/bin/sh" if Path("/bin/sh").exists() else "sh"
    return [sh, "-c", command]

"""Reading requests in the interactive session.

Line editing, ↑/↓ history (persisted across sessions), Ctrl+R search and redraw on
terminal resize come from the platform's readline (GNU readline or libedit). The
prompt's colour codes are wrapped in readline's ignore markers so wrapped long lines
and recalled history redraw cleanly.

Multi-line input: end a line with ``\\`` to continue it, or open a block with a line
containing only ``\"\"\"`` and close it the same way (for pasting logs or code).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable

from highhx.utils.paths import user_data_dir

HISTORY_FILE = "agent-input-history"
HISTORY_LENGTH = 1000
BLOCK = '"""'
PROMPT = "❯ "  # noqa: RUF001
CONTINUATION = "… "
PROMPT_MARKUP = f"[bold magenta]{PROMPT}[/bold magenta]"
CONTINUATION_MARKUP = f"[dim]{CONTINUATION}[/dim]"


def setup_readline() -> Callable[[], None]:
    """Load the input history; returns a function that saves it."""
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows without pyreadline
        return lambda: None
    path = user_data_dir() / HISTORY_FILE
    with contextlib.suppress(OSError):
        readline.read_history_file(str(path))
    readline.set_history_length(HISTORY_LENGTH)
    with contextlib.suppress(Exception):
        readline.parse_and_bind(tab_binding(readline.__doc__ or ""))

    def save() -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            readline.write_history_file(str(path))

    return save


def tab_binding(backend_doc: str) -> str:
    """Tab completes, in the syntax of the line editor Python was built with: macOS ships libedit,
    where GNU readline's "tab: complete" does nothing and Tab typed whitespace instead."""
    return "bind ^I rl_complete" if "libedit" in backend_doc else "tab: complete"


def ansi_prompt(text: str, sgr: str, *, color: bool) -> str:
    """``text`` coloured with SGR ``sgr``, with the escapes hidden from readline's width count."""
    if not color:
        return text
    return f"\001\033[{sgr}m\002{text}\001\033[0m\002"


def read_request(read_line: Callable[[str], str], *, prompt: str, continuation: str) -> str:
    """One request: a line, ``\\``-continued lines, or a ``\"\"\"`` block. Raises EOFError / KeyboardInterrupt."""
    first = read_line(prompt)
    if first.strip() == BLOCK:
        lines: list[str] = []
        while True:
            line = read_line(continuation)
            if line.strip() == BLOCK:
                return "\n".join(lines)
            lines.append(line)
    lines = [first]
    while lines[-1].endswith("\\"):
        lines[-1] = lines[-1][:-1]
        lines.append(read_line(continuation))
    return "\n".join(lines)

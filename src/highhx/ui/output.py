"""The single output channel used by commands.

* human mode: Rich formatting on stdout, warnings/errors on stderr
* ``--quiet``: only errors
* ``--json``: exactly one JSON document on stdout, nothing decorative
"""

from __future__ import annotations

import json
import re
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TextIO

from rich.console import Console, RenderableType
from rich.markup import escape

from highhx.core.result import CheckResult, CheckStatus
from highhx.security.secrets import Redactor
from highhx.ui.tables import key_value_table, make_table
from highhx.ui.terminal import Symbols, create_console, symbols_for

_UNESCAPED_MARKER = re.compile(r"(?<!\\)\[REDACTED\]")


class Output:
    """Thread-safe presenter for human and machine-readable output."""

    def __init__(
        self,
        *,
        json_mode: bool = False,
        quiet: bool = False,
        verbose: bool = False,
        no_color: bool = False,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        redactor: Redactor | None = None,
    ) -> None:
        self.json_mode = json_mode
        self.quiet = quiet
        self.verbose = verbose
        self._stdout = stdout or sys.stdout
        self._stderr = stderr or sys.stderr
        self.console: Console = create_console(no_color=no_color, file=self._stdout)
        self.err_console: Console = create_console(no_color=no_color, file=self._stderr)
        self.symbols: Symbols = symbols_for(self._stdout)
        self._lock = threading.RLock()
        self._json_emitted = False
        # Last line of defence: nothing printed through Output may contain a known secret.
        self.redactor = redactor or Redactor()

    # ------------------------------------------------------------------ helpers
    @property
    def human(self) -> bool:
        """True when decorative human output should be printed."""
        return not self.json_mode and not self.quiet

    def _safe(self, text: str) -> str:
        return escape(self.redactor.redact(text))

    def _print(self, renderable: RenderableType, *, err: bool = False, **kwargs: Any) -> None:
        if isinstance(renderable, str):
            # Redact, and keep the replacement marker from being read as Rich markup.
            renderable = _UNESCAPED_MARKER.sub(r"\\[REDACTED]", self.redactor.redact(renderable))
        with self._lock:
            (self.err_console if err else self.console).print(renderable, **kwargs)

    # -------------------------------------------------------------- messaging
    def success(self, message: str) -> None:
        if self.human:
            self._print(f"[ok]{self.symbols.ok}[/ok] {self._safe(message)}")

    def info(self, message: str) -> None:
        if self.human:
            self._print(f"[info]{self.symbols.info}[/info] {self._safe(message)}")

    def plain(self, message: str = "") -> None:
        if self.human:
            self._print(self._safe(message))

    def markup(self, message: str) -> None:
        """Print pre-formatted Rich markup (caller is responsible for escaping)."""
        if self.human:
            self._print(message)

    def note(self, message: str) -> None:
        if self.human:
            self._print(f"[muted]{self._safe(message)}[/muted]")

    def detail(self, message: str) -> None:
        """Only shown with --verbose."""
        if self.verbose and not self.json_mode and not self.quiet:
            self._print(f"[muted]  {self._safe(message)}[/muted]")

    def heading(self, title: str) -> None:
        if self.human:
            self._print(f"\n[title]{escape(title)}[/title]\n")

    def warn(self, message: str) -> None:
        if not self.json_mode and not self.quiet:
            self._print(f"[warn]{self.symbols.warn} {self._safe(message)}[/warn]", err=True)

    def error(self, message: str) -> None:
        if not self.json_mode:
            self._print(f"[fail]{self.symbols.fail} {self._safe(message)}[/fail]", err=True)

    def lines(self, lines: Iterable[str], *, empty: str | None = None) -> None:
        """Print raw lines, or an info message when there are none."""
        printed = False
        for line in lines:
            self.plain(line)
            printed = True
        if not printed and empty:
            self.info(empty)

    def outcomes(self, items: Iterable[tuple[bool, str]], *, empty: str | None = None) -> None:
        """Print ✓/✗ lines for ``(ok, message)`` pairs."""
        printed = False
        for ok, message in items:
            (self.success if ok else self.error)(message)
            printed = True
        if not printed and empty:
            self.info(empty)

    def print(self, renderable: RenderableType) -> None:
        if self.human:
            self._print(renderable)

    # ------------------------------------------------------------ structured
    def table(self, columns: Sequence[str], rows: Iterable[Sequence[Any]], *, title: str | None = None) -> None:
        if self.human:
            safe_rows = [[self.redactor.redact(str(c)) if c is not None else c for c in row] for row in rows]
            self._print(make_table(columns, safe_rows, title=title))

    def kv(self, data: dict[str, Any], *, title: str | None = None) -> None:
        if self.human:
            safe = {k: self.redactor.redact(v) if isinstance(v, str) else v for k, v in data.items()}
            self._print(key_value_table(safe, title=title))

    def status_symbol(self, status: CheckStatus | str) -> str:
        value = str(status)
        return {
            "ok": f"[ok]{self.symbols.ok}[/ok]",
            "success": f"[ok]{self.symbols.ok}[/ok]",
            "warn": f"[warn]{self.symbols.warn}[/warn]",
            "fail": f"[fail]{self.symbols.fail}[/fail]",
            "failed": f"[fail]{self.symbols.fail}[/fail]",
            "timeout": f"[fail]{self.symbols.fail}[/fail]",
            "cancelled": f"[warn]{self.symbols.warn}[/warn]",
            "skip": f"[muted]{self.symbols.skip}[/muted]",
            "skipped": f"[muted]{self.symbols.skip}[/muted]",
            "running": f"[info]{self.symbols.arrow}[/info]",
        }.get(value, self.symbols.info)

    def check(self, result: CheckResult) -> None:
        if not self.human:
            return
        message = f" — {escape(result.message)}" if result.message else ""
        self._print(f"{self.status_symbol(result.status)} {escape(result.name)}{message}")
        if result.hint and result.status in (CheckStatus.FAIL, CheckStatus.WARN):
            self._print(f"    [muted]{self.symbols.arrow} {escape(result.hint)}[/muted]")

    def checks(self, results: Iterable[CheckResult]) -> None:
        for result in results:
            self.check(result)

    # ---------------------------------------------------------------- streams
    def stream_line(self, line: str, *, stream: str = "stdout", source: str | None = None) -> None:
        """Echo a line of subprocess output (suppressed in JSON/quiet mode)."""
        if not self.human:
            return
        prefix = f"[muted]\\[{escape(source)}][/muted] " if source else ""
        text = self._safe(line)
        with self._lock:
            if stream == "stderr":
                self.err_console.print(f"{prefix}{text}", soft_wrap=True)
            else:
                self.console.print(f"{prefix}{text}", soft_wrap=True)

    # ------------------------------------------------------------------ JSON
    def json(self, data: Any) -> None:
        """Write one JSON document to stdout."""
        with self._lock:
            text = json.dumps(data, indent=2, default=str, sort_keys=False)
            self._stdout.write(self.redactor.redact(text) + "\n")
            self._stdout.flush()
            self._json_emitted = True

    @property
    def json_emitted(self) -> bool:
        return self._json_emitted

    def emit(self, data: Any, render: Callable[[], None] | None = None) -> None:
        """JSON mode: dump ``data``. Human mode: call ``render`` (if given)."""
        if self.json_mode:
            self.json(data)
        elif render is not None and not self.quiet:
            render()

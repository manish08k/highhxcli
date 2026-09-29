"""The task log: every run shows TASK, PLAN, OBSERVE, ACTION, RESULT, VERIFY, RECOVERY, FINAL.

    TASK:     Open YouTube, search for "adhento gani", and play the first result.
    PLAN:     3 steps (deterministic)
    OBSERVE:  YouTube <https://www.youtube.com/>
    ACTION:   navigate(value='https://www.youtube.com/results?search_query=adhento+gani')
    RESULT:   opened … (1.2s)
    VERIFY:   ✓ results are showing
    …
    FINAL:    ✓ Task completed (3 steps, 4.1s)

Entries go to every sink: the terminal renderer, the JSON output, and a JSONL file per task
(``<data dir>/tasks/<task id>.jsonl``) so a run can be read back later.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

KINDS = ("TASK", "PLAN", "OBSERVE", "ACTION", "RESULT", "VERIFY", "RECOVERY", "FINAL")


@dataclass(frozen=True)
class LogEntry:
    kind: str
    message: str
    ok: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "message": self.message, "at": round(self.at, 3)}
        if self.ok is not None:
            out["ok"] = self.ok
        if self.data:
            out["data"] = self.data
        return out

    def line(self, symbols: tuple[str, str] = ("✓", "✗")) -> str:
        mark = "" if self.ok is None else f"{symbols[0] if self.ok else symbols[1]} "
        return f"{self.kind + ':':<10}{mark}{self.message}"


Sink = Callable[[LogEntry], None]


class TaskLog:
    def __init__(self, *sinks: Sink, file: Path | None = None, redact: Callable[[str], str] | None = None) -> None:
        self.entries: list[LogEntry] = []
        self.sinks = list(sinks)
        self.file = file
        self.redact = redact or (lambda text: text)

    def add(self, kind: str, message: str, *, ok: bool | None = None, **data: Any) -> LogEntry:
        assert kind in KINDS, kind
        entry = LogEntry(kind, self.redact(message), ok, data)
        self.entries.append(entry)
        if self.file is not None:
            try:
                self.file.parent.mkdir(parents=True, exist_ok=True)
                with self.file.open("a", encoding="utf-8") as handle:
                    handle.write(self.redact(json.dumps(entry.to_dict(), default=str)) + "\n")
            except OSError:
                self.file = None  # the log file is a convenience; the run goes on without it
        for sink in self.sinks:
            sink(entry)
        return entry

    def kinds(self) -> list[str]:
        return [e.kind for e in self.entries]

    def text(self) -> str:
        return "\n".join(e.line() for e in self.entries)

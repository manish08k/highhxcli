"""Durable project memory: short facts the agent learns and reuses in later sessions.

In an initialized project the memory lives in ``.highhx/memory.md`` (plain
Markdown that can be reviewed and committed, so the whole team benefits);
otherwise in the per-user data directory, keyed by project path.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

from highhx.security.secrets import Redactor
from highhx.utils.filesystem import atomic_write_text
from highhx.utils.paths import user_data_dir

MAX_MEMORY_CHARS = 16_000
_SAVED = re.compile(r"\s*_\(saved [^)]*\)_\s*$")
HEADER = "# HighhX project memory\n\nFacts the HighhX agent keeps for this project. Edit freely.\n\n"


class ProjectMemory:
    def __init__(self, path: Path, redactor: Redactor | None = None) -> None:
        self.path = path
        self.redactor = redactor or Redactor()

    @classmethod
    def for_project(cls, root: Path, *, initialized: bool, redactor: Redactor | None = None) -> ProjectMemory:
        if initialized:
            return cls(root / ".highhx" / "memory.md", redactor)
        key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
        return cls(user_data_dir() / "agent" / "memory" / f"{key}.md", redactor)

    def read(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")[:MAX_MEMORY_CHARS]
        except OSError:
            return ""

    def facts(self) -> list[str]:
        return [_SAVED.sub("", line[2:]).strip() for line in self.read().splitlines() if line.startswith("- ")]

    def add(self, fact: str) -> str:
        fact = " ".join(self.redactor.redact(fact).split())[:500]
        if not fact:
            return "Nothing to remember."
        if "[REDACTED]" in fact:
            return "Not saved: the fact contained a secret value."
        existing = self.read()
        if any(f.lower() == fact.lower() for f in self.facts()):
            return "Already in project memory."
        text = existing or HEADER
        if not text.endswith("\n"):
            text += "\n"
        entry = f"- {fact} _(saved {date.today().isoformat()})_\n"
        if len(text) + len(entry) > MAX_MEMORY_CHARS:
            return f"Project memory is full ({MAX_MEMORY_CHARS} characters); ask the user to prune {self.path.name}."
        atomic_write_text(self.path, text + entry)
        return f"Saved to project memory ({self.path.name})."

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

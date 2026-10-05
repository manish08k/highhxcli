"""Durable project memory: short facts the agent learns and reuses in later sessions.

In an initialized project the memory lives in ``.highhx/memory.md`` (plain
Markdown that can be reviewed and committed, so the whole team benefits);
otherwise in the per-user data directory, keyed by project path.

Each entry has a type and a provenance (who saved it, from which task):

    - [strategy] The invoices page needs the "Billing" tab first _(saved 2026-10-04 · task_ab12)_

Types: ``task``, ``strategy`` (what worked), ``failure`` (what did not, and why), ``application``
and ``environment`` knowledge, ``preference`` (only the person can record one — never the agent),
and ``fact`` (untyped, the original format). Retrieval ranks entries against a task with the same
stemming and synonyms as trajectory search; retention removes old failures and task notes
(``prune``); any entry can be forgotten. Memory is **data, never instructions**: it reaches a model
only as bounded notes labelled that way, and secret values are refused when saving.
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
_ENTRY = re.compile(
    r"^- (?:\[(?P<kind>[a-z]+)\] )?(?P<text>.*?)(?:\s*_\(saved (?P<date>\d{4}-\d{2}-\d{2})(?: · (?P<source>[^)]*))?\)_)?\s*$"
)
KINDS = ("task", "strategy", "failure", "application", "environment", "preference", "fact")
RETENTION_DAYS = {"failure": 90, "task": 180, "strategy": 365, "application": 365, "environment": 365}
"""Days an entry of each type is kept by ``prune`` (preferences and facts stay until forgotten)."""
KIND_WEIGHT = {
    "strategy": 1.0,
    "failure": 0.9,
    "application": 0.8,
    "environment": 0.8,
    "preference": 1.0,
    "task": 0.6,
    "fact": 0.7,
}
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
        return [entry["text"] for entry in self.entries()]

    def entries(self) -> list[dict[str, str]]:
        """Every entry: index (1-based), kind, text, saved (date), source."""
        out: list[dict[str, str]] = []
        for line in self.read().splitlines():
            if not line.startswith("- "):
                continue
            match = _ENTRY.match(line)
            if match is None:
                continue
            kind = match.group("kind") if match.group("kind") in KINDS else "fact"
            text = (
                match.group("text")
                if match.group("kind") in KINDS or not match.group("kind")
                else _SAVED.sub("", line[2:]).strip()
            )
            out.append(
                {
                    "index": str(len(out) + 1),
                    "kind": kind,
                    "text": text.strip(),
                    "saved": match.group("date") or "",
                    "source": match.group("source") or "",
                }
            )
        return out

    def relevant(self, query: str, *, limit: int = 5) -> list[str]:
        """The entries most related to ``query`` (shared terms, weighted by type), as notes."""
        from highhx.trajectories.search import terms

        wanted = set(terms(query))
        scored = []
        for entry in self.entries():
            have = set(terms(entry["text"]))
            if not wanted or not have:
                continue
            overlap = len(wanted & have) / len(wanted | have)
            if overlap > 0:
                scored.append((overlap * KIND_WEIGHT.get(entry["kind"], 0.7), entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [f"[{e['kind']}] {e['text']}" for _score, e in scored[:limit]]

    def add(self, fact: str, *, kind: str = "fact", source: str = "agent") -> str:
        if kind not in KINDS:
            return f"Not saved: the type must be one of {', '.join(KINDS)}."
        if kind == "preference" and source != "user":
            return "Not saved: only the person can record a preference."
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
        label = "" if kind == "fact" else f"[{kind}] "
        origin = re.sub(r"[^\w .:-]", "", source)[:60]
        entry = f"- {label}{fact} _(saved {date.today().isoformat()}" + (f" · {origin}" if origin else "") + ")_\n"
        if len(text) + len(entry) > MAX_MEMORY_CHARS:
            return f"Project memory is full ({MAX_MEMORY_CHARS} characters); ask the user to prune {self.path.name}."
        atomic_write_text(self.path, text + entry)
        return f"Saved to project memory ({self.path.name})."

    def forget(self, index: int) -> bool:
        """Remove entry ``index`` (1-based, as ``entries()`` numbers them)."""
        lines = self.read().splitlines(keepends=True)
        seen = 0
        for position, line in enumerate(lines):
            if line.startswith("- ") and _ENTRY.match(line.rstrip("\n")):
                seen += 1
                if seen == index:
                    del lines[position]
                    atomic_write_text(self.path, "".join(lines))
                    return True
        return False

    def prune(self, today: date | None = None) -> int:
        """Remove entries older than their type's retention; returns how many."""
        today = today or date.today()
        lines = self.read().splitlines(keepends=True)
        kept, removed = [], 0
        for line in lines:
            match = _ENTRY.match(line.rstrip("\n")) if line.startswith("- ") else None
            kind = (match.group("kind") if match else "") or "fact"
            saved = match.group("date") if match else None
            days = RETENTION_DAYS.get(kind)
            if days is not None and saved and (today - date.fromisoformat(saved)).days > days:
                removed += 1
                continue
            kept.append(line)
        if removed:
            atomic_write_text(self.path, "".join(kept))
        return removed

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

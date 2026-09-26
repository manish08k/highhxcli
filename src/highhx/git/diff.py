"""Diff statistics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class FileChange:
    path: str
    added: int | None
    deleted: int | None

    @property
    def binary(self) -> bool:
        return self.added is None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"binary": self.binary}


def parse_numstat(text: str) -> list[FileChange]:
    changes = []
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, deleted, path = parts[0], parts[1], parts[2]
        changes.append(FileChange(path, None if added == "-" else int(added), None if deleted == "-" else int(deleted)))
    return changes

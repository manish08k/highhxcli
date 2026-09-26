"""Branch listing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

FIELD = "\x1f"
BRANCH_FORMAT = FIELD.join(
    [
        "%(refname:short)",
        "%(upstream:short)",
        "%(objectname:short)",
        "%(committerdate:iso-strict)",
        "%(HEAD)",
        "%(upstream:track)",
    ]
)


@dataclass
class Branch:
    name: str
    upstream: str
    commit: str
    date: str
    current: bool
    track: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_branches(text: str) -> list[Branch]:
    branches = []
    for line in text.splitlines():
        if not line.strip():
            continue
        name, upstream, commit, date, head, track = (line.split(FIELD) + [""] * 6)[:6]
        branches.append(Branch(name, upstream, commit, date, head.strip() == "*", track))
    return branches

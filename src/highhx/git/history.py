"""``git log`` parsing."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from highhx.git.commits import ConventionalCommit, parse_conventional

FIELD = "\x1f"
RECORD = "\x1e"
LOG_FORMAT = FIELD.join(["%H", "%h", "%an", "%ae", "%aI", "%s", "%b"]) + RECORD


@dataclass
class Commit:
    sha: str
    short: str
    author: str
    email: str
    date: str
    subject: str
    body: str = ""

    @property
    def conventional(self) -> ConventionalCommit | None:
        return parse_conventional(self.subject, self.body)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        cc = self.conventional
        data["type"] = cc.type if cc else None
        data["breaking"] = cc.breaking if cc else False
        return data


def parse_log(text: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in text.split(RECORD):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(FIELD)
        if len(parts) < 7:
            parts += [""] * (7 - len(parts))
        commits.append(Commit(*[p.strip() for p in parts[:7]]))
    return commits

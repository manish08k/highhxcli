"""``git status --porcelain=v2 --branch`` parsing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FileStatus:
    path: str
    index: str
    worktree: str
    original: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GitStatus:
    branch: str | None = None
    commit: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    staged: list[FileStatus] = field(default_factory=list)
    unstaged: list[FileStatus] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)

    @property
    def detached(self) -> bool:
        return self.branch in (None, "(detached)")

    @property
    def clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked or self.conflicted)

    @property
    def change_count(self) -> int:
        paths = (
            {f.path for f in self.staged} | {f.path for f in self.unstaged} | set(self.untracked) | set(self.conflicted)
        )
        return len(paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "branch": self.branch,
            "commit": self.commit,
            "upstream": self.upstream,
            "ahead": self.ahead,
            "behind": self.behind,
            "clean": self.clean,
            "detached": self.detached,
            "staged": [f.to_dict() for f in self.staged],
            "unstaged": [f.to_dict() for f in self.unstaged],
            "untracked": self.untracked,
            "conflicted": self.conflicted,
        }


def parse_status_v2(text: str) -> GitStatus:
    status = GitStatus()
    for line in text.split("\0") if "\0" in text else text.splitlines():
        if not line:
            continue
        if line.startswith("# branch.oid "):
            oid = line.split(" ", 2)[2]
            status.commit = None if oid == "(initial)" else oid
        elif line.startswith("# branch.head "):
            status.branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            status.upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            status.ahead = abs(int(parts[2]))
            status.behind = abs(int(parts[3]))
        elif line.startswith("1 ") or line.startswith("2 "):
            fields = line.split(" ", 9 if line.startswith("2 ") else 8)
            xy = fields[1]
            if line.startswith("2 "):
                path_part = fields[9]
                path, _, orig = path_part.partition("\t")
                original: str | None = orig or None
            else:
                path, original = fields[8], None
            entry = FileStatus(path, xy[0], xy[1], original)
            if xy[0] != ".":
                status.staged.append(entry)
            if xy[1] != ".":
                status.unstaged.append(entry)
        elif line.startswith("u "):
            status.conflicted.append(line.split(" ", 10)[10])
        elif line.startswith("? "):
            status.untracked.append(line[2:])
    return status

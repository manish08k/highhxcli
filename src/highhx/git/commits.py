"""Conventional Commit parsing and validation."""

from __future__ import annotations

import re
from dataclasses import dataclass

CONVENTIONAL_RE = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s+(?P<description>.+)$")
TYPES = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "style",
    "revert",
    "security",
    "deps",
)


@dataclass(frozen=True)
class ConventionalCommit:
    type: str
    scope: str | None
    description: str
    breaking: bool


def parse_conventional(subject: str, body: str = "") -> ConventionalCommit | None:
    match = CONVENTIONAL_RE.match(subject.strip())
    if not match:
        return None
    breaking = bool(match.group("bang")) or bool(re.search(r"^BREAKING[ -]CHANGE:", body, re.MULTILINE))
    return ConventionalCommit(
        match.group("type").lower(), match.group("scope"), match.group("description").strip(), breaking
    )


def validate_message(message: str, *, conventional: bool = False) -> list[str]:
    """Problems with a commit message (empty list if acceptable)."""
    problems: list[str] = []
    lines = message.strip().splitlines()
    if not lines or not lines[0].strip():
        return ["commit message is empty"]
    subject = lines[0]
    if len(subject) > 100:
        problems.append(f"subject is {len(subject)} characters (keep it under 100)")
    if len(lines) > 1 and lines[1].strip():
        problems.append("separate the subject from the body with a blank line")
    if conventional:
        parsed = parse_conventional(subject)
        if parsed is None:
            problems.append("subject is not a Conventional Commit (e.g. 'feat(api): add login')")
        elif parsed.type not in TYPES:
            problems.append(f"unknown commit type '{parsed.type}' (expected one of {', '.join(TYPES)})")
    return problems

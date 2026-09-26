"""Changelog generation from real git history (Keep a Changelog style)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from highhx.git.history import Commit
from highhx.utils.filesystem import atomic_write_text, read_text

SECTIONS = (
    ("breaking", "⚠ Breaking Changes"),
    ("feat", "Features"),
    ("fix", "Bug Fixes"),
    ("perf", "Performance"),
    ("security", "Security"),
    ("refactor", "Refactoring"),
    ("docs", "Documentation"),
    ("deps", "Dependencies"),
    ("other", "Other Changes"),
)
SKIP_TYPES = {"chore", "ci", "test", "style", "build"}
HEADER = "# Changelog\n\nAll notable changes to this project are documented in this file.\n"


def group_commits(commits: list[Commit], *, include_all: bool = False) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {key: [] for key, _ in SECTIONS}
    for commit in commits:
        if commit.subject.startswith(("Merge ", "chore(release)")):
            continue
        cc = commit.conventional
        if cc is None:
            groups["other"].append(f"{commit.subject} ({commit.short})")
            continue
        scope = f"**{cc.scope}:** " if cc.scope else ""
        line = f"{scope}{cc.description} ({commit.short})"
        if cc.breaking:
            groups["breaking"].append(line)
        if cc.type in groups and cc.type not in ("breaking", "other"):
            groups[cc.type].append(line)
        elif cc.type in SKIP_TYPES and not include_all:
            continue
        elif not cc.breaking:
            groups["other"].append(line)
    return {k: v for k, v in groups.items() if v}


def render_section(version: str, commits: list[Commit], *, on: date | None = None, include_all: bool = False) -> str:
    day = (on or date.today()).isoformat()
    lines = [f"## [{version}] - {day}", ""]
    groups = group_commits(commits, include_all=include_all)
    if not groups:
        lines += ["No user-facing changes.", ""]
    for key, title in SECTIONS:
        if key in groups:
            lines.append(f"### {title}")
            lines.append("")
            lines += [f"- {item}" for item in groups[key]]
            lines.append("")
    return "\n".join(lines)


def insert_section(path: Path, section: str) -> None:
    """Insert ``section`` above the newest release (after an Unreleased section, if any)."""
    text = read_text(path) if path.is_file() else HEADER
    match = re.search(r"^## \[(?!Unreleased)", text, re.MULTILINE)
    if match:
        new = text[: match.start()] + section.rstrip() + "\n\n" + text[match.start() :]
    else:
        new = text.rstrip() + "\n\n" + section.rstrip() + "\n"
    atomic_write_text(path, new)

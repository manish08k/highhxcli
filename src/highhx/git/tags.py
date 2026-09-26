"""Tag listing and version-tag selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

FIELD = "\x1f"
TAG_FORMAT = FIELD.join(["%(refname:short)", "%(creatordate:iso-strict)", "%(objectname:short)", "%(subject)"])


@dataclass
class Tag:
    name: str
    date: str
    commit: str
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_tags(text: str) -> list[Tag]:
    tags = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = ([*line.split(FIELD), "", "", "", ""])[:4]
        tags.append(Tag(*parts))
    return tags


def latest_version_tag(tags: list[Tag], prefix: str = "v") -> Tag | None:
    """Highest semantic-version tag with ``prefix``."""
    from highhx.release.versioning import SemVer

    best: tuple[SemVer, Tag] | None = None
    for tag in tags:
        if not tag.name.startswith(prefix):
            continue
        try:
            version = SemVer.parse(tag.name[len(prefix) :])
        except ValueError:
            continue
        if best is None or version > best[0]:
            best = (version, tag)
    return best[1] if best else None

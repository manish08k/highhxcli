"""Project root discovery and workspace members."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from highhx.config.defaults import CONFIG_DIR, CONFIG_FILE
from highhx.project.monorepo import detect_monorepo, expand_member_globs
from highhx.utils.paths import find_upwards


def find_project_root(start: Path) -> Path | None:
    """Nearest ancestor containing ``.highhx/config.yaml``."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / CONFIG_DIR / CONFIG_FILE).is_file():
            return candidate
    return None


def find_git_root(start: Path) -> Path | None:
    return find_upwards(start, (".git",))


def resolve_root(start: Path) -> tuple[Path, bool]:
    """Return ``(root, initialized)``: an initialized project root, else the git root, else ``start``."""
    initialized = find_project_root(start)
    if initialized is not None:
        return initialized, True
    git_root = find_git_root(start)
    return (git_root or start.resolve()), False


@dataclass
class WorkspaceMember:
    path: str
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "name": self.name}


def workspace_members(root: Path, configured: list[str] | None = None) -> list[WorkspaceMember]:
    """Members from config (globs) or monorepo detection."""
    if configured:
        paths = expand_member_globs(root, configured)
    else:
        info = detect_monorepo(root)
        paths = info.members if info else []
    return [WorkspaceMember(path=p, name=Path(p).name) for p in paths]

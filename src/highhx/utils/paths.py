"""Path helpers: per-user directories and project-root discovery."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from highhx.utils.platform import IS_MACOS, IS_WINDOWS

APP_NAME = "highhx"
PROJECT_DIR_NAME = ".highhx"


def _home() -> Path:
    return Path(os.environ.get("HIGHHX_HOME_OVERRIDE") or Path.home())


def user_config_dir() -> Path:
    """Directory for user-level configuration (respects ``HIGHHX_CONFIG_DIR``)."""
    override = os.environ.get("HIGHHX_CONFIG_DIR")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or _home() / "AppData" / "Roaming") / APP_NAME
    if IS_MACOS:
        return _home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME") or _home() / ".config") / APP_NAME


def user_data_dir() -> Path:
    """Directory for user-level data such as global history and plugins."""
    override = os.environ.get("HIGHHX_DATA_DIR")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or _home() / "AppData" / "Local") / APP_NAME
    if IS_MACOS:
        return _home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or _home() / ".local" / "share") / APP_NAME


def user_cache_dir() -> Path:
    """Directory for disposable cache data."""
    override = os.environ.get("HIGHHX_CACHE_DIR")
    if override:
        return Path(override)
    if IS_WINDOWS:
        return user_data_dir() / "cache"
    if IS_MACOS:
        return _home() / "Library" / "Caches" / APP_NAME
    return Path(os.environ.get("XDG_CACHE_HOME") or _home() / ".cache") / APP_NAME


def find_upwards(start: Path, names: Iterable[str], stop: Path | None = None) -> Path | None:
    """Walk from ``start`` towards the filesystem root and return the first directory
    that contains any of ``names``."""
    wanted = tuple(names)
    current = start.resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if any((candidate / name).exists() for name in wanted):
            return candidate
        if stop is not None and candidate == stop.resolve():
            break
    return None


def is_within(path: Path, root: Path) -> bool:
    """Return True when ``path`` resolves to a location inside ``root``."""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def display_path(path: Path, root: Path | None = None) -> str:
    """Return a short, forward-slash path suitable for output."""
    base = root or Path.cwd()
    try:
        rel = path.resolve().relative_to(base.resolve())
        text = rel.as_posix()
        return text or "."
    except ValueError:
        return path.as_posix()

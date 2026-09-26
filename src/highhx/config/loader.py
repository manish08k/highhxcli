"""Loading configuration files (with profile overlays)."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from highhx.config.defaults import CONFIG_DIR, CONFIG_FILE, PROFILES_DIR
from highhx.config.schema import HighhXConfig
from highhx.config.validation import validate_config
from highhx.core.errors import ConfigError


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (PyYAML silently keeps the last one)."""


def _construct_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            continue
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark, f"found duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def parse_yaml(text: str) -> Any:
    """Safe YAML parsing that also rejects duplicate keys."""
    return yaml.load(text, Loader=_StrictLoader)


def load_yaml(path: Path) -> Any:
    """Parse a YAML (or JSON) file, raising :class:`ConfigError` with location info on failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc.strerror or exc}") from exc
    try:
        return parse_yaml(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark is not None else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ConfigError(f"Invalid YAML in {path.name}{where}: {problem}") from exc


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into a copy of ``base`` (lists are replaced)."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


@dataclass
class LoadedConfig:
    """The effective configuration plus where it came from."""

    config: HighhXConfig
    path: Path | None
    profile: str | None = None
    sources: list[Path] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)


def config_path(root: Path) -> Path:
    return root / CONFIG_DIR / CONFIG_FILE


def available_profiles(root: Path) -> list[str]:
    directory = root / CONFIG_DIR / PROFILES_DIR
    if not directory.is_dir():
        return []
    return sorted(p.stem for p in directory.glob("*.yaml"))


def read_config_data(root: Path, profile: str | None = None) -> tuple[dict[str, Any], list[Path]]:
    """Read raw config data, applying ``.highhx/profiles/<profile>.yaml`` if requested."""
    path = config_path(root)
    sources: list[Path] = []
    data: dict[str, Any] = {}
    if path.exists():
        loaded = load_yaml(path)
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"{path.name} must contain a mapping at the top level")
        data = loaded or {}
        sources.append(path)
    if profile:
        overlay_path = root / CONFIG_DIR / PROFILES_DIR / f"{profile}.yaml"
        if not overlay_path.exists():
            known = ", ".join(available_profiles(root)) or "none"
            raise ConfigError(
                f"Config profile '{profile}' not found.",
                hint=f"Available profiles: {known}. Create one with `highhx profile create {profile}`.",
            )
        overlay = load_yaml(overlay_path) or {}
        if not isinstance(overlay, dict):
            raise ConfigError(f"{overlay_path.name} must contain a mapping")
        data = deep_merge(data, overlay)
        sources.append(overlay_path)
    return data, sources


def load_config(root: Path, *, profile: str | None = None) -> LoadedConfig:
    """Load, validate and parse the configuration for the project at ``root``."""
    profile = profile or os.environ.get("HIGHHX_PROFILE") or None
    data, sources = read_config_data(root, profile)
    errors = validate_config(data)
    if errors:
        raise ConfigError(
            f"Invalid configuration ({len(errors)} problem{'s' if len(errors) != 1 else ''})",
            details=errors,
            hint="Fix .highhx/config.yaml and run `highhx config validate`.",
        )
    path = config_path(root)
    return LoadedConfig(
        config=HighhXConfig.from_dict(data),
        path=path if path.exists() else None,
        profile=profile,
        sources=sources,
        data=data,
    )

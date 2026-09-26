"""Collected plugin contributions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.plugins.manifest import PluginCommand, PluginManifest


@dataclass
class LoadedPlugin:
    manifest: PluginManifest
    scope: str
    code_loaded: bool = False
    status: str = "enabled"
    problem: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "scope": self.scope,
            "code_loaded": self.code_loaded,
            "status": self.status,
            "problem": self.problem,
        }


@dataclass
class PluginRegistry:
    plugins: list[LoadedPlugin] = field(default_factory=list)
    click_commands: dict[str, tuple[str, Any]] = field(default_factory=dict)
    declarative_commands: dict[str, tuple[PluginManifest, PluginCommand]] = field(default_factory=dict)
    detectors: list[tuple[str, Any]] = field(default_factory=list)
    workflow_dirs: list[tuple[Path, str]] = field(default_factory=list)
    template_dirs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def add_click_command(self, plugin: str, command: Any) -> None:
        name = getattr(command, "name", None)
        if not name:
            self.errors.append(f"{plugin}: command without a name ignored")
            return
        self.click_commands[name] = (plugin, command)

    def command_names(self) -> list[str]:
        return sorted({*self.click_commands, *self.declarative_commands})

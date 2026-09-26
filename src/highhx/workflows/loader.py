"""Discovering and loading workflow files."""

from __future__ import annotations

import builtins
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from highhx.config.loader import load_yaml
from highhx.core.errors import ConfigError, NotFoundError, WorkflowError
from highhx.utils.validation import did_you_mean
from highhx.workflows.parser import normalize_document, parse_workflow
from highhx.workflows.schema import WorkflowSpec

WORKFLOW_SUFFIXES = (".yaml", ".yml", ".json")


@dataclass
class WorkflowRef:
    """A workflow file that has been discovered (not necessarily valid)."""

    key: str
    path: Path
    name: str | None = None
    description: str = ""
    triggers: tuple[str, ...] = ()
    source: str = "project"
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "path": str(self.path),
            "source": self.source,
            "triggers": list(self.triggers),
            "error": self.error,
        }


class WorkflowLoader:
    """Loads workflows from the project's ``.highhx/workflows`` plus extra directories
    (e.g. contributed by plugins). Project workflows win on name clashes."""

    def __init__(self, directories: Iterable[tuple[Path, str]]) -> None:
        self.directories = [(Path(d), src) for d, src in directories]

    @classmethod
    def for_project(cls, workflows_dir: Path, extra: Iterable[tuple[Path, str]] = ()) -> WorkflowLoader:
        return cls([(workflows_dir, "project"), *extra])

    def _files(self) -> list[tuple[Path, str]]:
        seen: set[str] = set()
        files: list[tuple[Path, str]] = []
        for directory, source in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.iterdir()):
                if path.suffix in WORKFLOW_SUFFIXES and path.is_file() and path.stem not in seen:
                    seen.add(path.stem)
                    files.append((path, source))
        return files

    def list(self) -> builtins.list[WorkflowRef]:
        refs: builtins.list[WorkflowRef] = []
        for path, source in self._files():
            ref = WorkflowRef(key=path.stem, path=path, source=source)
            try:
                data = normalize_document(load_yaml(path))
                if isinstance(data, dict):
                    ref.name = str(data.get("name") or path.stem)
                    ref.description = str(data.get("description") or "")
                    on = data.get("on") or []
                    ref.triggers = tuple([on] if isinstance(on, str) else [str(x) for x in on])
                else:
                    ref.error = "not a mapping"
            except ConfigError as exc:
                ref.error = exc.message
            refs.append(ref)
        return refs

    def keys(self) -> builtins.list[str]:
        return [path.stem for path, _ in self._files()]

    def __contains__(self, name: object) -> bool:
        """``"build" in loader`` — is there a workflow file with this key?"""
        return isinstance(name, str) and name in self.keys()

    def find(self, name: str) -> Path:
        """Resolve a workflow by file stem, ``name:`` field, or path."""
        candidate = Path(name)
        if candidate.suffix in WORKFLOW_SUFFIXES and candidate.is_file():
            return candidate
        files = self._files()
        for path, _ in files:
            if path.stem == name:
                return path
        for ref in self.list():
            if ref.name == name:
                return ref.path
        keys = [p.stem for p, _ in files]
        raise NotFoundError(
            f"Workflow '{name}' not found{did_you_mean(name, keys)}.",
            hint=f"Available workflows: {', '.join(keys) or 'none'}. Create one with `highhx workflow create`.",
        )

    def load(self, name: str) -> WorkflowSpec:
        return self.load_path(self.find(name))

    @staticmethod
    def load_path(path: Path) -> WorkflowSpec:
        data = load_yaml(path)
        if data is None:
            raise WorkflowError(f"Workflow file {path.name} is empty.")
        return parse_workflow(data, source=path, key=path.stem)

"""Discovering and loading plugins."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from highhx.config.schema import PluginsConfig
from highhx.core.errors import PluginError
from highhx.detection import Detection
from highhx.plugins.interface import HighhXPlugin, PluginAPI
from highhx.plugins.manifest import PluginManifest, load_manifest, manifest_path
from highhx.plugins.registry import LoadedPlugin, PluginRegistry
from highhx.plugins.sandbox import TrustStore, integrity_problem


def plugin_directories(base: Path) -> list[Path]:
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and manifest_path(p) is not None)


def _declarative_detector(manifest: PluginManifest):  # type: ignore[no-untyped-def]
    def detect(root: Path) -> list[Detection]:
        found = []
        for detector in manifest.detectors:
            evidence = [f for f in detector.files if list(root.glob(f))]
            if evidence:
                found.append(Detection(detector.name, detector.kind, evidence, details={"plugin": manifest.name}))
        return found

    return detect


def _load_code(manifest: PluginManifest, registry: PluginRegistry) -> None:
    assert manifest.entry
    file, class_name = manifest.entry.split(":", 1)
    module_name = f"highhx_plugin_{manifest.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, manifest.directory / file)
    if spec is None or spec.loader is None:
        raise PluginError(f"Cannot import {file} from plugin '{manifest.name}'.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(f"Plugin '{manifest.name}' failed to import: {type(exc).__name__}: {exc}") from exc
    cls = getattr(module, class_name, None)
    if cls is None or not isinstance(cls, type) or not issubclass(cls, HighhXPlugin):
        raise PluginError(f"Plugin '{manifest.name}': {class_name} is not a HighhXPlugin subclass.")
    try:
        cls().register(PluginAPI(registry, manifest.name, manifest.permissions))
    except PluginError:
        raise
    except Exception as exc:
        raise PluginError(f"Plugin '{manifest.name}' failed during register(): {type(exc).__name__}: {exc}") from exc


def load_plugins(
    sources: list[tuple[Path, Path, str]], settings: PluginsConfig, trust: TrustStore | None = None
) -> PluginRegistry:
    """Load plugins from ``(plugins_dir, lock_file, scope)`` sources. Project scope wins on name clashes."""
    registry = PluginRegistry()
    seen: set[str] = set()
    for base, _lock_path, scope in sources:
        for directory in plugin_directories(base):
            try:
                manifest = load_manifest(directory)
            except PluginError as exc:
                registry.errors.append(f"{directory.name}: {exc.message}: {'; '.join(exc.details)}")
                continue
            if manifest.name in seen:
                continue
            seen.add(manifest.name)
            loaded = LoadedPlugin(manifest, scope)
            registry.plugins.append(loaded)
            if manifest.name in settings.disabled or (settings.enabled and manifest.name not in settings.enabled):
                loaded.status = "disabled"
                continue
            for rel in manifest.workflow_dirs:
                registry.workflow_dirs.append((directory / rel, f"plugin:{manifest.name}"))
            for rel in manifest.template_dirs:
                registry.template_dirs.append(directory / rel)
            for command in manifest.commands:
                registry.declarative_commands.setdefault(command.name, (manifest, command))
            if manifest.detectors:
                registry.detectors.append((manifest.name, _declarative_detector(manifest)))
            if manifest.has_code:
                if not settings.allow_code:
                    loaded.problem = "contains code; set plugins.allow_code: true to run it"
                    continue
                problem = integrity_problem(manifest, trust)
                if problem:
                    loaded.status = "blocked"
                    loaded.problem = problem
                    registry.errors.append(f"{manifest.name}: code not loaded — {problem}")
                    continue
                try:
                    _load_code(manifest, registry)
                    loaded.code_loaded = True
                except PluginError as exc:
                    loaded.status = "error"
                    loaded.problem = exc.message
                    registry.errors.append(exc.message)
    return registry

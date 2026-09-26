"""The public plugin API.

A code plugin exposes a class (named by ``entry`` in its manifest) that
subclasses :class:`HighhXPlugin` and implements :meth:`HighhXPlugin.register`::

    from highhx.plugins.interface import HighhXPlugin, PluginAPI
    import click

    class Plugin(HighhXPlugin):
        def register(self, api: PluginAPI) -> None:
            @click.command("hello")
            def hello() -> None:
                click.echo("hello from a plugin")

            api.add_command(hello)
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from highhx.plugins.registry import PluginRegistry

Detector = Callable[[Path], list[Any]]


class PluginAPI:
    """What a plugin may contribute. Each method checks the plugin's declared permissions."""

    def __init__(self, registry: PluginRegistry, plugin_name: str, permissions: list[str]) -> None:
        self._registry = registry
        self._plugin = plugin_name
        self._permissions = set(permissions)

    def _need(self, permission: str) -> None:
        from highhx.core.errors import PluginError

        if permission not in self._permissions:
            raise PluginError(f"Plugin '{self._plugin}' needs the '{permission}' permission for this contribution.")

    def add_command(self, command: Any) -> None:
        """Register a :class:`click.Command` as a top-level ``highhx`` command."""
        self._need("commands")
        self._registry.add_click_command(self._plugin, command)

    def add_detector(self, detector: Detector) -> None:
        """Register ``detector(root) -> list[Detection]``."""
        self._need("detectors")
        self._registry.detectors.append((self._plugin, detector))

    def add_deployment_strategy(self, type_name: str, factory: Callable[..., Any]) -> None:
        """Register a deployment strategy usable as ``type: <type_name>``."""
        self._need("deploy")
        from highhx.deployment.strategy import register_strategy

        register_strategy(type_name, factory)

    def add_cloud_provider(self, name: str, factory: Callable[[], Any]) -> None:
        """Register a provider for ``type: plugin:<name>`` targets."""
        self._need("deploy")
        from highhx.integrations.cloud import register_provider

        register_provider(name, factory)

    def add_workflow_dir(self, path: Path) -> None:
        self._registry.workflow_dirs.append((path, f"plugin:{self._plugin}"))

    def add_template_dir(self, path: Path) -> None:
        self._registry.template_dirs.append(path)


class HighhXPlugin(abc.ABC):
    """Base class for code plugins."""

    name: str = ""

    @abc.abstractmethod
    def register(self, api: PluginAPI) -> None:
        """Contribute commands, detectors, strategies … through ``api``."""

"""Provider registry used by plugins."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CloudProvider(Protocol):
    """Interface a plugin-provided deployment backend implements."""

    name: str

    def deploy(self, target: Any, context: dict[str, Any]) -> dict[str, Any]:
        """Deploy and return details (must raise on failure)."""
        ...

    def rollback(self, target: Any, previous: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]: ...

    def status(self, target: Any, context: dict[str, Any]) -> dict[str, Any]: ...


_providers: dict[str, Callable[[], CloudProvider]] = {}
_lock = threading.Lock()


def register_provider(name: str, factory: Callable[[], CloudProvider]) -> None:
    with _lock:
        _providers[name] = factory


def get_provider(name: str) -> CloudProvider | None:
    with _lock:
        factory = _providers.get(name)
    return factory() if factory else None


def provider_names() -> list[str]:
    with _lock:
        return sorted(_providers)

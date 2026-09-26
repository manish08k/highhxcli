"""Secret handling for environment values (masking only — values are never printed)."""

from __future__ import annotations

from collections.abc import Mapping

from highhx.security.secrets import is_placeholder, is_secret_key, mask


def masked(values: Mapping[str, str], secret_keys: set[str] | None = None) -> dict[str, str]:
    """Copy of ``values`` with secret values masked."""
    explicit = secret_keys or set()
    return {k: mask(v) if (k in explicit or is_secret_key(k)) else v for k, v in values.items()}


def is_secret(name: str, declared: bool | None = None) -> bool:
    if declared is not None:
        return declared
    return is_secret_key(name)


__all__ = ["is_placeholder", "is_secret", "mask", "masked"]

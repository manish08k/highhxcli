"""Provider registry: names → factories, default models and credential sources."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from highhx.agent.model.base import ModelProvider
from highhx.core.errors import ConfigError


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    default_model: str
    models: tuple[str, ...]
    key_env: tuple[str, ...]
    """Environment variables holding a bring-your-own key (empty for the HighhX gateway)."""
    extra: str | None
    """pip extra that installs the provider SDK."""


def _catalog() -> dict[str, ProviderInfo]:
    from highhx.agent.model import anthropic, gemini, openai

    return {
        "highhx": ProviderInfo(
            "highhx", "HighhX (managed)", "auto", ("auto", *anthropic.MODELS, *openai.MODELS, *gemini.MODELS), (), None
        ),
        "anthropic": ProviderInfo(
            "anthropic", "Anthropic", anthropic.DEFAULT_MODEL, anthropic.MODELS, ("ANTHROPIC_API_KEY",), "anthropic"
        ),
        "openai": ProviderInfo("openai", "OpenAI", openai.DEFAULT_MODEL, openai.MODELS, ("OPENAI_API_KEY",), "openai"),
        "gemini": ProviderInfo(
            "gemini",
            "Google Gemini",
            gemini.DEFAULT_MODEL,
            gemini.MODELS,
            ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
            "gemini",
        ),
    }


PROVIDER_NAMES = ("highhx", "anthropic", "openai", "gemini")
UPSTREAM_NAMES = ("anthropic", "openai", "gemini")

_extra_factories: dict[str, Callable[[str | None], ModelProvider]] = {}


def register_provider(name: str, factory: Callable[[str | None], ModelProvider]) -> None:
    """Register an additional direct provider (plugins, self-hosted models). ``factory(api_key)``."""
    _extra_factories[name] = factory


def provider_info(name: str) -> ProviderInfo:
    catalog = _catalog()
    if name not in catalog:
        known = ", ".join([*PROVIDER_NAMES, *_extra_factories])
        raise ConfigError(f"Unknown AI provider '{name}'.", hint=f"Choose one of: {known}.")
    return catalog[name]


def api_key_for(name: str) -> str | None:
    info = _catalog().get(name)
    for var in info.key_env if info else ():
        if os.environ.get(var):
            return os.environ[var]
    return None


def create_direct_provider(name: str, api_key: str | None = None) -> ModelProvider:
    """A provider that talks to the vendor directly (bring your own key)."""
    if name in _extra_factories:
        return _extra_factories[name](api_key)
    key = api_key or api_key_for(name)
    if name == "anthropic":
        from highhx.agent.model.anthropic import AnthropicProvider

        return AnthropicProvider(key)
    if name == "openai":
        from highhx.agent.model.openai import OpenAIProvider

        return OpenAIProvider(key)
    if name == "gemini":
        from highhx.agent.model.gemini import GeminiProvider

        return GeminiProvider(key)
    provider_info(name)  # raises a helpful error for unknown names
    raise ConfigError(f"'{name}' is not a direct provider.")

"""What a model can take and produce — so the agent and the vision operator know, before sending
anything, whether screenshots can be shown to it and how it answers.

Known vendors are described here; a self-hosted (``local``) model says what it is through
configuration (``computer.vision.vision: true``), with a conservative guess from its name
otherwise. Nothing is inferred from a failed request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

VISION_NAME = re.compile(r"vl\b|vl-|vision|ui-?tars|llava|pixtral|gemma-?3|minicpm-v|qwen2\.5-?vl|qwen3-?vl|internvl|molmo")
"""Self-hosted model names that are vision-language models."""


@dataclass(frozen=True)
class ModelCapabilities:
    provider: str
    model: str
    vision: bool
    """Images (screenshots, attached pictures, PDF pages) can be sent."""
    max_images: int
    context_tokens: int
    action_format: str
    """How it states a GUI action: ``tools`` (function calling), ``uitars`` (``click(start_box=…)``)
    or ``json``."""
    coordinates: str
    """Where its GUI coordinates live: ``pixels`` of the image it saw, or ``relative1000``."""
    local: bool
    """The model runs on this computer: images and files sent to it do not leave it."""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def is_loopback(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def capabilities_for(provider: str, model: str | None, *, config: dict[str, Any] | None = None) -> ModelCapabilities:
    """The capabilities of ``model`` on ``provider`` (``config``: a local model's own settings)."""
    config = config or {}
    name = (model or "").lower()
    if provider == "local":
        uitars = "tars" in name
        vision = bool(config["vision"]) if "vision" in config else bool(VISION_NAME.search(name))
        return ModelCapabilities(
            provider,
            model or "",
            vision,
            max_images=int(config.get("max_images") or 2),
            context_tokens=int(config.get("context_tokens") or 32_000),
            action_format=str(config.get("format") or ("uitars" if uitars else "json")),
            coordinates=str(config.get("coordinates") or ("relative1000" if uitars else "pixels")),
            local=is_loopback(str(config.get("base_url") or "")),
        )
    if provider in ("highhx", "anthropic", "openai", "gemini"):
        # Every model these route to takes images (the managed gateway routes to them too).
        context = {"anthropic": 200_000, "openai": 400_000, "gemini": 1_000_000}.get(provider, 200_000)
        return ModelCapabilities(
            provider, model or "auto", True, 4, context, "tools", "pixels", local=False
        )
    return ModelCapabilities(provider, model or "", False, 0, 32_000, "tools", "pixels", local=False)

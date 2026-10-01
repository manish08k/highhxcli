"""Which model the vision agent sees the screen with.

    computer:                      # .highhx/config.yaml (or the HIGHHX_VISION_* environment variables)
      vision:
        provider: local            # local (an OpenAI-compatible server) · highhx (HighhX Pro)
        base_url: http://127.0.0.1:11434/v1
        model: qwen2.5vl:7b        # or ui-tars-1.5-7b, …
        coordinates: pixels        # or relative1000 (UI-TARS 1.0-style grids)
        format: json               # or uitars

A ``local`` model on this computer keeps every screenshot here. Any other model — a remote
``base_url`` or HighhX Pro's gateway — receives screenshots, so the caller must have the person's
consent (``remote_ok``) before one is built; nothing is uploaded silently.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from highhx.agent.model.capabilities import ModelCapabilities, capabilities_for
from highhx.core.errors import ConfigError, PlanRequiredError

if TYPE_CHECKING:
    from highhx.agent.model.base import ModelProvider
    from highhx.commands import App
    from highhx.execution.cancellation import CancellationToken

ENV = {
    "provider": "HIGHHX_VISION_PROVIDER",
    "model": "HIGHHX_VISION_MODEL",
    "base_url": "HIGHHX_VISION_BASE_URL",
    "coordinates": "HIGHHX_VISION_COORDINATES",
    "format": "HIGHHX_VISION_FORMAT",
}
KEY_ENV = "HIGHHX_VISION_API_KEY"


def vision_config(app: App) -> dict[str, Any]:
    """``computer.vision`` from the project configuration, overridden by the environment."""
    section: dict[str, Any] = {}
    if app.initialized:
        computer = app.config.raw.get("computer")
        if isinstance(computer, dict) and isinstance(computer.get("vision"), dict):
            section = dict(computer["vision"])
    for key, var in ENV.items():
        if os.environ.get(var):
            section[key] = os.environ[var]
    if section.get("base_url") and not section.get("provider"):
        section["provider"] = "local"
    return section


def vision_model(app: App, cancel: CancellationToken | None) -> tuple[ModelProvider, ModelCapabilities]:
    """The configured vision model and what it can do (see the module docstring)."""
    config = vision_config(app)
    provider_name = str(config.get("provider") or "highhx")
    if provider_name == "local":
        from highhx.agent.model.openai import OpenAIProvider

        base_url = str(config.get("base_url") or "")
        model = str(config.get("model") or "")
        if not base_url or not model:
            raise ConfigError(
                "A local vision model needs base_url and model.",
                hint="e.g. HIGHHX_VISION_BASE_URL=http://127.0.0.1:11434/v1 HIGHHX_VISION_MODEL=qwen2.5vl:7b",
            )
        provider = OpenAIProvider(os.environ.get(KEY_ENV) or "local", base_url=base_url)
        return provider, capabilities_for("local", model, config={**config, "base_url": base_url})
    return _pro_model(app, provider_name, config.get("model"), cancel)


def _pro_model(
    app: App, upstream: str, model: Any, cancel: CancellationToken | None
) -> tuple[ModelProvider, ModelCapabilities]:
    from highhx.agent.bootstrap import build_provider, resolve_settings
    from highhx.cloud.plans import AGENT_COMPUTER_USE
    from highhx.core.errors import AccountError, CloudError

    hint = (
        "`highhx login` for HighhX Pro, or run a local vision model: "
        "HIGHHX_VISION_BASE_URL=http://127.0.0.1:11434/v1 HIGHHX_VISION_MODEL=<model>"
    )
    cloud = app.cloud
    if not cloud.signed_in:
        raise PlanRequiredError("Seeing and operating the screen with AI needs a vision model.", hint=hint)
    try:
        account = cloud.require(AGENT_COMPUTER_USE, what="AI computer use")
    except (AccountError, CloudError) as exc:
        raise PlanRequiredError(exc.message, hint=hint) from None
    if account.cached:
        raise PlanRequiredError("The HighhX platform is unreachable, so the Pro model cannot start.", hint=hint)
    overrides = {"provider": upstream, **({"model": str(model)} if model else {})}
    settings = resolve_settings(app, account, overrides)
    provider = build_provider(settings, cloud, account)
    return provider, capabilities_for(settings.provider, settings.model)


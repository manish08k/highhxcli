"""The configured models, built with HighhX's existing rules (nothing new to configure twice).

    vision    computer.vision in .highhx/config.yaml or HIGHHX_VISION_* (see
              highhx.computer.operator.models): a local OpenAI-compatible server, or HighhX Pro
    language  HIGHHX_PLANNER_BASE_URL + HIGHHX_PLANNER_MODEL for a local model, otherwise the
              configured vision model when it is local, otherwise HighhX Pro

A remote model receives screenshots and task text, so building one needs ``remote_ok`` (the
person's consent, asked by the command that wants it). Free users without a local model get
:class:`~highhx.core.errors.PlanRequiredError` with the hint to run one locally.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from highhx.core.errors import ConfigError, PlanRequiredError
from highhx.models.adapters import ChatLanguageModel, ChatVisionModel

if TYPE_CHECKING:
    from highhx.commands import App
    from highhx.execution.cancellation import CancellationToken

PLANNER_ENV = {"base_url": "HIGHHX_PLANNER_BASE_URL", "model": "HIGHHX_PLANNER_MODEL"}


def vision_model(app: App, cancel: CancellationToken | None = None, *, remote_ok: bool = False) -> ChatVisionModel:
    from highhx.computer.operator.models import vision_model as configured

    provider, capabilities = configured(app, cancel)
    if not capabilities.local and not remote_ok:
        raise PlanRequiredError(
            "Vision grounding would send screenshots to a remote model.",
            hint="Confirm with --remote-vision, or run a local vision model (HIGHHX_VISION_BASE_URL).",
        )
    return ChatVisionModel(ChatLanguageModel(provider, capabilities))


def language_model(app: App, cancel: CancellationToken | None = None, *, remote_ok: bool = False) -> ChatLanguageModel:
    from highhx.agent.model.capabilities import capabilities_for

    base_url = os.environ.get(PLANNER_ENV["base_url"], "")
    model = os.environ.get(PLANNER_ENV["model"], "")
    if base_url or model:
        if not (base_url and model):
            raise ConfigError(
                "A local planner model needs both HIGHHX_PLANNER_BASE_URL and HIGHHX_PLANNER_MODEL.",
                hint="e.g. HIGHHX_PLANNER_BASE_URL=http://127.0.0.1:11434/v1 HIGHHX_PLANNER_MODEL=qwen2.5:14b",
            )
        from highhx.agent.model.capabilities import is_loopback
        from highhx.agent.model.openai import OpenAIProvider

        if not is_loopback(base_url) and not remote_ok:
            raise PlanRequiredError(
                f"HIGHHX_PLANNER_BASE_URL ({base_url}) is not on this computer: the task and screen contents would leave it.",
                hint="Confirm with --remote-model, or point it at a model served locally (127.0.0.1).",
            )
        local = OpenAIProvider(os.environ.get("HIGHHX_PLANNER_API_KEY") or "local", base_url=base_url)
        return ChatLanguageModel(local, capabilities_for("local", model, config={"base_url": base_url}))
    from highhx.computer.operator.models import vision_config
    from highhx.computer.operator.models import vision_model as configured

    if str(vision_config(app).get("provider") or "") == "local":
        provider, capabilities = configured(app, cancel)
        return ChatLanguageModel(provider, capabilities)
    if not remote_ok:
        raise PlanRequiredError(
            "Planning with AI would send the task and screen contents to a remote model.",
            hint="Confirm with --remote-model, or run a local model (HIGHHX_PLANNER_BASE_URL, HIGHHX_PLANNER_MODEL).",
        )
    provider, capabilities = configured(app, cancel)  # HighhX Pro (raises PlanRequiredError when not entitled)
    return ChatLanguageModel(provider, capabilities)

"""The HighhX AI gateway.

Resolves which upstream provider and model serve a request, enforces the plan
and monthly allowance, streams normalised events back to the CLI as SSE and
meters usage. The provider adapters are the same ones the CLI uses for
bring-your-own-key (``highhx.agent.model``), so behaviour is identical.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from highhx.agent.model.base import ModelProvider, ModelRequest
from highhx.agent.model.registry import UPSTREAM_NAMES, create_direct_provider, provider_info
from highhx.agent.streaming import Completed, to_wire
from highhx.core.errors import ModelProviderError, OperationCancelledError
from highhx_platform.config import Settings
from highhx_platform.streams import StreamRun

log = logging.getLogger(__name__)

ProviderFactory = Callable[[str, str], ModelProvider]
UsageRecorder = Callable[[str, str, Completed | None, str], None]
"""(provider, model, completed-or-None, status) → persist usage."""


@dataclass(frozen=True)
class Route:
    provider: str
    model: str


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.status, self.code, self.message, self.hint = status, code, message, hint


def model_owner(model: str) -> str | None:
    for name in UPSTREAM_NAMES:
        if model in provider_info(name).models:
            return name
    return None


def resolve_route(
    settings: Settings, *, requested_provider: str | None, requested_model: str | None, account_upstream: str | None
) -> Route:
    """Pick the upstream: explicit provider > the model's owner > account setting > platform default."""
    model = requested_model if requested_model not in (None, "", "auto") else None
    if requested_provider and requested_provider not in UPSTREAM_NAMES:
        raise GatewayError(400, "invalid_provider", f"Unknown provider '{requested_provider}'.")
    owner = model_owner(model) if model else None
    if model and owner is None:
        raise GatewayError(
            400, "invalid_model", f"Model '{model}' is not available through HighhX.", "See `highhx agent models`."
        )
    provider = requested_provider or owner or account_upstream or settings.default_provider
    if owner is not None and owner != provider:
        raise GatewayError(400, "invalid_model", f"Model '{model}' is not served by {provider}.")
    if provider not in settings.provider_keys:
        available = [p for p in UPSTREAM_NAMES if p in settings.provider_keys]
        if requested_provider or owner or not available:
            raise GatewayError(
                503,
                "provider_unavailable",
                f"{provider_info(provider).label} is not available on this HighhX platform right now.",
                f"Available: {', '.join(available) or 'none'}.",
            )
        provider = available[0]
    return Route(provider, model or provider_info(provider).default_model)


def fallback_routes(settings: Settings, primary: Route, *, pinned_model: bool) -> list[Route]:
    """Primary route, then configured fallbacks (only when the client did not pin a model)."""
    routes = [primary]
    if pinned_model:
        return routes
    for name in settings.fallback_providers:
        if name in settings.provider_keys and name != primary.provider:
            routes.append(Route(name, provider_info(name).default_model))
    return routes


def run_upstream(
    job: StreamRun,
    request: ModelRequest,
    routes: list[Route],
    settings: Settings,
    factory: ProviderFactory,
    record: UsageRecorder,
) -> None:
    """Worker: call the upstream once (falling back only if it failed before producing any
    output), buffer events into ``job`` and meter the result exactly once."""
    request.max_tokens = min(request.max_tokens, settings.max_output_tokens)
    completed: Completed | None = None
    status = "error"
    route = routes[0]
    try:
        for index, route in enumerate(routes):
            request.model = route.model
            produced = False
            try:
                provider = factory(route.provider, settings.provider_keys[route.provider])
                for event in provider.stream(request, cancel=job.cancel):
                    if job.cancel.cancelled:
                        raise OperationCancelledError("cancelled")
                    produced = True
                    if isinstance(event, Completed):
                        completed = event
                    job.append(*to_wire(event))
                if completed is None and job.cancel.cancelled:
                    raise OperationCancelledError("cancelled")
                status = "ok" if completed is not None else "error"
                if completed is None:
                    job.append(
                        "error",
                        {"code": "upstream_error", "message": "The model stream ended early.", "retryable": True},
                    )
                break
            except ModelProviderError as exc:
                last = index == len(routes) - 1
                if not produced and exc.retryable and not last and not job.cancel.cancelled:
                    log.warning("upstream %s failed (%s); falling back", route.provider, exc.message)
                    continue
                log.warning("upstream %s failed: %s", route.provider, exc.message)
                job.append(
                    "error",
                    {"code": "upstream_error", "message": exc.message, "hint": exc.hint, "retryable": exc.retryable},
                )
                break
    except OperationCancelledError:
        status = "cancelled"
        job.append("error", {"code": "cancelled", "message": "The request was cancelled.", "retryable": False})
    except Exception:  # never leak internals to the client
        log.exception("gateway failure")
        job.append("error", {"code": "internal", "message": "The HighhX AI gateway failed.", "retryable": True})
    finally:
        if job.cancel.cancelled and status != "ok":
            status = "cancelled"
        try:
            record(route.provider, route.model, completed, status)
        except Exception:
            log.exception("could not record usage")
        job.finish(status)


def default_factory(name: str, key: str) -> ModelProvider:
    return create_direct_provider(name, key)

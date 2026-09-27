"""FastAPI application factory."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from highhx import __version__ as cli_version
from highhx.agent.model.registry import UPSTREAM_NAMES
from highhx.cloud import protocol
from highhx_platform import __version__
from highhx_platform.config import Settings
from highhx_platform.db import Database
from highhx_platform.gateway import ProviderFactory, default_factory
from highhx_platform.routers import account, ai, auth, billing, web
from highhx_platform.security import Limits
from highhx_platform.streams import StreamStore

log = logging.getLogger("highhx_platform")

PROTOCOL_EXEMPT = ("/v1/meta", "/v1/billing/webhook", "/v1/admin/")
"""Endpoints not called by the CLI (Stripe webhooks, support tools, capability discovery)."""

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'",
}


def create_app(settings: Settings | None = None, *, provider_factory: ProviderFactory | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    database = Database(settings.database_url)
    if settings.auto_migrate:
        database.migrate()
    app = FastAPI(title="HighhX Platform", version=__version__, docs_url="/docs", redoc_url=None)
    app.state.settings = settings
    app.state.db = database
    app.state.provider_factory = provider_factory or default_factory
    app.state.streams = StreamStore(database, grace=settings.stream_resume_grace, retention=settings.stream_retention)
    app.state.limits = Limits(
        database,
        login=settings.login_attempts_per_15min,
        signup=settings.signups_per_hour,
        device=settings.device_starts_per_hour,
    )

    @app.middleware("http")
    async def protocol_and_security_headers(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path.startswith("/v1/") and not path.startswith(PROTOCOL_EXEMPT):
            _version, error = protocol.check_client(request.headers.get(protocol.PROTOCOL_HEADER))
            if error is not None:
                shown = request.headers.get(protocol.PROTOCOL_HEADER) or "none"
                rejected = JSONResponse(
                    {
                        "detail": {
                            "code": error,
                            "message": f"This HighhX client (protocol {shown}) is not supported by this platform "
                            f"(protocol {protocol.PROTOCOL_VERSION}).",
                            "hint": "Update with `pip install -U highhxcli`.",
                        }
                    },
                    status_code=426,
                )
                rejected.headers[protocol.PROTOCOL_HEADER] = protocol.PROTOCOL_VERSION
                return rejected
        response: Response = await call_next(request)
        response.headers[protocol.PROTOCOL_HEADER] = protocol.PROTOCOL_VERSION
        for key, value in SECURITY_HEADERS.items():
            if key == "Content-Security-Policy" and request.url.path.startswith(("/docs", "/openapi")):
                continue  # the interactive API docs load their own assets
            response.headers.setdefault(key, value)
        if request.url.scheme == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        """Liveness: the process serves requests."""
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        """Readiness: the database answers and its schema is at the latest migration."""
        try:
            current, head = database.schema_revision(), database.head_revision()
        except Exception as exc:
            return JSONResponse({"status": "unavailable", "database": f"{type(exc).__name__}"}, status_code=503)
        ok = current == head
        return JSONResponse(
            {"status": "ok" if ok else "migrations_pending", "schema": current, "head": head},
            status_code=200 if ok else 503,
        )

    @app.get("/v1/meta")
    def meta() -> dict[str, Any]:
        return {
            "name": "HighhX Platform",
            "version": __version__,
            "cli_version": cli_version,
            "providers": [p for p in UPSTREAM_NAMES if p in settings.provider_keys],
            "billing": settings.billing_enabled,
            "protocol": protocol.PROTOCOL_VERSION,
            "signup": settings.signup_enabled,
        }

    @app.get("/", include_in_schema=False)
    def root() -> JSONResponse:
        return JSONResponse({"name": "HighhX Platform", "docs": "/docs", "cli": "pip install highhxcli"})

    for router in (auth.router, account.router, ai.router, billing.router, web.router):
        app.include_router(router)
    if settings.cors_origins:
        # The CLI does not need CORS; only enable it for explicitly listed web origins.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", protocol.PROTOCOL_HEADER, protocol.IDEMPOTENCY_HEADER],
        )
    return app

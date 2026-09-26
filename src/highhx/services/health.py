"""Service health evaluation."""

from __future__ import annotations

from highhx.config.schema import ServiceConfig
from highhx.deployment.health import http_probe
from highhx.services.ports import is_port_open


def service_healthy(service: ServiceConfig) -> tuple[bool | None, str]:
    """(healthy, message); healthy is None when no check is defined."""
    if service.health and service.health.url:
        return http_probe(
            service.health.url, timeout=min(service.health.timeout, 3.0), expected=service.health.expected_status
        )
    if service.port:
        open_ = is_port_open(service.port)
        return open_, f"port {service.port} {'open' if open_ else 'closed'}"
    return None, "no health check"

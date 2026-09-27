"""HTTP client for the HighhX platform API (stdlib only, streaming-capable)."""

from __future__ import annotations

import contextlib
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

from highhx import __version__
from highhx.cloud import protocol
from highhx.cloud.sse import ServerEvent, decode
from highhx.core.errors import (
    AccountError,
    CloudError,
    ConfigError,
    HighhXError,
    OperationCancelledError,
    PlanRequiredError,
    QuotaExceededError,
)
from highhx.execution.cancellation import CancellationToken

USER_AGENT = f"highhx-cli/{__version__}"


def _error_payload(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {"message": body[:300]} if body else {}
    if isinstance(data, dict):
        detail = data.get("detail")
        if isinstance(detail, dict):
            return detail
        if isinstance(detail, str):
            return {"message": detail}
        return data
    return {}


def http_error(status: int, payload: dict[str, Any], url: str) -> HighhXError:
    """Map a platform error response to a HighhX error with an actionable hint."""
    error = _http_error(status, payload, url)
    if isinstance(error, CloudError):
        error.status = status
        error.code = str(payload["code"]) if payload.get("code") else None
    return error


def _http_error(status: int, payload: dict[str, Any], url: str) -> HighhXError:
    code = str(payload.get("code") or "")
    message = str(payload.get("message") or f"HighhX platform returned HTTP {status}")
    if status == 426 or code in ("client_outdated", "client_unsupported"):
        return CloudError(
            message if payload.get("message") else "This HighhX CLI is not compatible with the HighhX platform.",
            hint=str(payload.get("hint") or "Update with `pip install -U highhxcli`."),
        )
    if status == 401:
        return AccountError(
            message if code else "You are not signed in to HighhX (or your session expired).",
            hint="Run `highhx login`.",
        )
    if code == "plan_required" or status == 402:
        return PlanRequiredError(message, hint=str(payload.get("hint") or "Upgrade with `highhx account upgrade`."))
    if code == "quota_exceeded":
        return QuotaExceededError(message, hint=str(payload.get("hint") or "See `highhx account usage`."))
    if status == 429:
        return CloudError(message, hint=str(payload.get("hint") or "Slow down and try again shortly."))
    if status >= 500:
        if code:  # the platform explained what is wrong (e.g. provider_unavailable)
            return CloudError(message, hint=str(payload.get("hint") or "Try again shortly."))
        return CloudError(f"The HighhX platform had a problem ({status}): {message}", hint="Try again shortly.")
    return CloudError(message, hint=payload.get("hint") and str(payload["hint"]), details=[f"{status} {url}"])


class PlatformClient:
    """Small JSON + SSE client. Every request carries the bearer token when one is set."""

    def __init__(self, base_url: str, token: str | None = None, *, timeout: float = 30.0) -> None:
        if not base_url.startswith(("https://", "http://")):
            raise ConfigError(f"Invalid HighhX platform URL: {base_url!r}", hint="Use https://host[:port].")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        *,
        accept: str,
        headers: dict[str, str] | None = None,
    ) -> urllib.request.Request:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": accept,
            protocol.PROTOCOL_HEADER: protocol.PROTOCOL_VERSION,
            protocol.CLIENT_HEADER: USER_AGENT,
            **(headers or {}),
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)

    def _open(self, request: urllib.request.Request, timeout: float) -> Any:
        try:
            # Scheme is restricted to http(s) in __init__ (and to https for non-local hosts by credentials).
            response = urllib.request.urlopen(request, timeout=timeout)  # nosec B310
        except urllib.error.HTTPError as exc:
            payload = _error_payload(exc)
            exc.close()
            raise http_error(exc.code, payload, request.full_url) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CloudError(
                f"Cannot reach the HighhX platform at {self.base_url} ({reason}).",
                hint="Check your connection, or set HIGHHX_API_URL if you use a self-hosted platform.",
            ) from None
        announced = response.headers.get(protocol.PROTOCOL_HEADER)
        if announced is not None and not protocol.compatible_server(announced):
            response.close()
            raise CloudError(
                f"The HighhX platform at {self.base_url} speaks protocol {announced}; this CLI speaks "
                f"{protocol.PROTOCOL_VERSION}.",
                hint="Update the CLI (`pip install -U highhxcli`) or use a compatible platform.",
            )
        return response

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._open(self._request(method, path, body, accept="application/json"), self.timeout) as response:
            raw = response.read()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise CloudError("The HighhX platform returned an invalid response.") from None
        return data if isinstance(data, dict) else {"items": data}

    def get(self, path: str) -> dict[str, Any]:
        return self.request("GET", path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("POST", path, body or {})

    def patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("PATCH", path, body)

    def stream(
        self,
        path: str,
        body: dict[str, Any],
        *,
        timeout: float = 180.0,
        headers: dict[str, str] | None = None,
        cancel: CancellationToken | None = None,
    ) -> Iterator[ServerEvent]:
        """POST and yield server-sent events until the server closes the stream.

        ``timeout`` is the idle timeout per read (the platform sends keep-alives). Cancelling
        ``cancel`` closes the connection immediately, which unblocks a pending read.
        """
        response = self._open(self._request("POST", path, body, accept="text/event-stream", headers=headers), timeout)

        def _close(_reason: str) -> None:
            with contextlib.suppress(Exception):
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)  # unblock a read in another thread
            with contextlib.suppress(Exception):
                response.close()

        if cancel is not None:
            cancel.on_cancel(_close)
        with response:
            try:
                yield from decode(response)
            except (TimeoutError, ConnectionError, OSError, ValueError, AttributeError) as exc:
                if cancel is not None and cancel.cancelled:
                    raise OperationCancelledError("Streaming cancelled.") from None
                raise CloudError(f"Lost connection to the HighhX platform ({exc}).", hint="Try again.") from None
            if cancel is not None and cancel.cancelled:
                raise OperationCancelledError("Streaming cancelled.")

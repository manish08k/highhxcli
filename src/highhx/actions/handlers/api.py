"""``api.request``: one HTTP(S) request, as an action.

Risk follows what the request can do: a read of this computer (loopback) is LOW, a read of
another host is MEDIUM (data in the URL leaves the machine), and anything that changes state
(POST, PUT, PATCH, DELETE) or uses a credential is HIGH. Credentials come from environment
variables named in ``headers_from_env``. Their values are registered with the redactor and never
logged. Policy names are ``network:<host>`` (and ``credential:use``), so a policy can allow or
deny hosts.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from highhx.actions.policy import Risk
from highhx.actions.spec import ActionContext, ActionResult, Inputs
from highhx.agent.model.capabilities import is_loopback
from highhx.agent.tools.base import ToolError

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
MAX_BODY = 64_000
SHOWN_HEADERS = ("content-type", "content-length", "location", "etag", "last-modified")


def method_of(inputs: Inputs) -> str:
    return str(inputs.get("method") or "GET").upper()


def risk_for(inputs: Inputs) -> Risk:
    url = str(inputs.get("url") or "")
    if inputs.get("headers_from_env") or method_of(inputs) not in SAFE_METHODS:
        return Risk.HIGH
    return Risk.LOW if is_loopback(url) else Risk.MEDIUM


def policy_name(inputs: Inputs) -> str:
    if inputs.get("headers_from_env"):
        return "credential:use"
    return f"network:{urlparse(str(inputs.get('url') or '')).hostname or 'unknown'}"


def request(ctx: ActionContext, inputs: Inputs) -> ActionResult:
    url = str(inputs["url"])
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ToolError(f"only http(s) URLs can be requested, not {url!r}")
    method = method_of(inputs)
    headers = {str(k): str(v) for k, v in (inputs.get("headers") or {}).items()}
    secrets: list[str] = []
    for header, variable in (inputs.get("headers_from_env") or {}).items():
        value = os.environ.get(str(variable))
        if value is None:
            raise ToolError(f"environment variable {variable} is not set")
        headers[str(header)] = value
        secrets.append(value)
    if secrets:
        ctx.app.engine.redactor.add(secrets)
    body: bytes | None = None
    if inputs.get("json") is not None:
        body = json.dumps(inputs["json"]).encode()
        headers.setdefault("Content-Type", "application/json")
    elif inputs.get("body") is not None:
        body = str(inputs["body"]).encode()
    timeout = float(inputs.get("timeout") or 30)
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            raw = response.read(MAX_BODY + 1)
            response_headers = {k.lower(): v for k, v in response.headers.items()}
    except urllib.error.HTTPError as exc:
        with exc:  # an error response is still a response: read it, then release the connection
            status = exc.code
            raw = exc.read(MAX_BODY + 1) if exc.fp is not None else b""
            response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return ActionResult(False, error=f"{method} {url} failed: {getattr(exc, 'reason', exc)}", summary="request failed")
    text = raw[:MAX_BODY].decode("utf-8", errors="replace")
    output: dict[str, Any] = {
        "status": status,
        "url": url,
        "method": method,
        "headers": {k: v for k, v in response_headers.items() if k in SHOWN_HEADERS},
        "body": text,
        "truncated": len(raw) > MAX_BODY,
        "network": [{"url": url, "method": method, "status": status}],
    }
    if "json" in response_headers.get("content-type", ""):
        try:
            output["json"] = json.loads(text)
        except ValueError:
            pass
    expected = inputs.get("expect_status")
    ok = (status == int(expected)) if expected is not None else 200 <= status < 400
    return ActionResult(
        ok,
        output=output,
        summary=f"{method} {parsed.hostname}{parsed.path or '/'} → {status}",
        verified=ok,
        error="" if ok else f"{method} {url} returned {status}",
        retryable=method in SAFE_METHODS,
    )

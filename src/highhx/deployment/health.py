"""Health checks (HTTP or command) with retries."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from highhx.config.schema import HealthCheckConfig
from highhx.core.engine import Engine
from highhx.execution.cancellation import CancellationToken
from highhx.execution.command import CommandSpec


@dataclass
class HealthResult:
    ok: bool
    attempts: int
    message: str
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def http_probe(url: str, *, timeout: float, expected: int) -> tuple[bool, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "highhx-health-check"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{url}: {reason}"
    if status == expected:
        return True, f"{url} returned {status}"
    return False, f"{url} returned {status} (expected {expected})"


def run_health_check(
    engine: Engine,
    check: HealthCheckConfig | None,
    *,
    cancel: CancellationToken | None = None,
    sleeper: Any = None,
) -> HealthResult:
    if check is None:
        return HealthResult(True, 0, "no health check configured", skipped=True)
    if engine.dry_run:
        return HealthResult(True, 0, "dry run: health check not executed", skipped=True)
    token = cancel or engine.ctx.cancel
    message = ""
    for attempt in range(1, max(1, check.retries) + 1):
        if check.url:
            ok, message = http_probe(check.url, timeout=check.timeout, expected=check.expected_status)
        else:
            assert check.command is not None
            result = engine.capture(CommandSpec(check.command, timeout=check.timeout, name="health-check"))
            ok = result.ok
            message = f"`{check.command}` exited {result.exit_code}" + (
                f": {result.stderr.strip().splitlines()[-1]}" if result.stderr.strip() and not ok else ""
            )
        if ok:
            return HealthResult(True, attempt, message)
        if attempt < check.retries:
            if sleeper is not None:
                sleeper(check.interval)
            elif token.wait(check.interval):
                return HealthResult(False, attempt, "cancelled while waiting for health check")
    return HealthResult(False, check.retries, message)

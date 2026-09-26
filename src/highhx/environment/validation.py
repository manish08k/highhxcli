"""Environment validation (``highhx env check``)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from highhx.core.result import CheckResult, CheckStatus
from highhx.environment.profiles import ResolvedEnvironment
from highhx.environment.variables import EnvironmentSpec
from highhx.security.secrets import is_placeholder, is_secret_key


def check_environment(
    spec: EnvironmentSpec,
    resolved: ResolvedEnvironment,
    *,
    process_env: Mapping[str, str] | None = None,
) -> list[CheckResult]:
    """Validate required variables, patterns and choices. Values are never included."""
    environ = process_env if process_env is not None else os.environ
    checks: list[CheckResult] = []
    profile = resolved.profile
    definition = spec.profile(profile)
    for error in resolved.errors:
        checks.append(
            CheckResult("env file", CheckStatus.FAIL, error, hint="Fix the syntax of the .env file.", category="env")
        )
    required = {name for name, v in spec.variables.items() if v.required_in(profile)} | set(definition.required)
    for name in sorted(required):
        value = resolved.values.get(name, environ.get(name))
        if value is None or value == "":
            checks.append(
                CheckResult(
                    name,
                    CheckStatus.FAIL,
                    "required but not set",
                    hint=f"Set it with `highhx env set {name} --profile {profile}` or export it in your shell.",
                    category="env",
                )
            )
            continue
        source = resolved.sources.get(name, "shell environment")
        variable = spec.variables.get(name)
        secret = variable.secret if variable and variable.secret is not None else is_secret_key(name)
        if secret and is_placeholder(value):
            checks.append(
                CheckResult(name, CheckStatus.WARN, f"looks like a placeholder value (from {source})", category="env")
            )
        else:
            checks.append(CheckResult(name, CheckStatus.OK, f"set (from {source})", category="env"))
    for name, variable in sorted(spec.variables.items()):
        value = resolved.values.get(name, environ.get(name))
        if value is None or value == "":
            continue
        if variable.pattern and not re.search(variable.pattern, value):
            checks.append(
                CheckResult(name, CheckStatus.FAIL, f"does not match pattern {variable.pattern}", category="env")
            )
        if variable.choices and value not in variable.choices:
            checks.append(
                CheckResult(name, CheckStatus.FAIL, f"must be one of: {', '.join(variable.choices)}", category="env")
            )
    if not checks:
        checks.append(
            CheckResult(f"profile {profile}", CheckStatus.OK, "no required variables declared", category="env")
        )
    return checks

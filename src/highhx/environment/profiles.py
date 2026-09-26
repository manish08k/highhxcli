"""Environment profile resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from highhx.environment.dotenv import DotEnvError, load_dotenv
from highhx.environment.variables import EnvironmentSpec

PROFILE_ENV_VAR = "HIGHHX_ENV"


@dataclass
class ResolvedEnvironment:
    """Variables for one profile and where each came from."""

    profile: str
    values: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    files_found: list[str] = field(default_factory=list)
    files_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def resolve_profile(root: Path, spec: EnvironmentSpec, profile: str) -> ResolvedEnvironment:
    """Merge declared defaults, the profile's .env files (in order) and its inline env."""
    result = ResolvedEnvironment(profile=profile)
    for name, variable in spec.variables.items():
        if variable.default is not None:
            result.values[name] = variable.default
            result.sources[name] = "default"
    definition = spec.profile(profile)
    for relative in definition.files:
        path = root / relative
        if not path.is_file():
            result.files_missing.append(relative)
            continue
        try:
            loaded = load_dotenv(path, environ={**os.environ, **result.values})
        except DotEnvError as exc:
            result.errors.append(f"{relative}: {exc}")
            continue
        result.files_found.append(relative)
        for key, value in loaded.items():
            result.values[key] = value
            result.sources[key] = relative
    for key, value in definition.env.items():
        result.values[key] = value
        result.sources[key] = "environment.yaml"
    return result

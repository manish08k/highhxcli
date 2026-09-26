"""Environment management service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.errors import NotFoundError, ValidationError
from highhx.core.result import CheckResult
from highhx.environment.dotenv import set_dotenv_value, unset_dotenv_value
from highhx.environment.profiles import PROFILE_ENV_VAR, ResolvedEnvironment, resolve_profile
from highhx.environment.secrets import is_secret, mask
from highhx.environment.validation import check_environment
from highhx.environment.variables import EnvironmentSpec
from highhx.storage.cache import StateStore
from highhx.utils.filesystem import ensure_gitignore_entries
from highhx.utils.validation import did_you_mean, is_env_name

ACTIVE_PROFILE_KEY = "env.active_profile"


@dataclass
class EnvDiff:
    left: str
    right: str
    only_left: list[str] = field(default_factory=list)
    only_right: list[str] = field(default_factory=list)
    changed: list[dict[str, str]] = field(default_factory=list)
    same: int = 0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class EnvironmentManager:
    """Reads, validates and edits environment profiles."""

    def __init__(
        self, root: Path, spec: EnvironmentSpec, state: StateStore | None = None, *, override: str | None = None
    ) -> None:
        self.root = root
        self.spec = spec
        self.state = state
        self.override = override

    # ------------------------------------------------------------ profiles
    def active_profile(self) -> str:
        if self.override:
            return self.override
        from_env = os.environ.get(PROFILE_ENV_VAR)
        if from_env:
            return from_env
        if self.state is not None:
            stored = self.state.get(ACTIVE_PROFILE_KEY)
            if stored:
                return str(stored)
        return self.spec.default_profile

    def profile_names(self) -> list[str]:
        names = list(self.spec.profiles)
        if self.spec.default_profile not in names:
            names.insert(0, self.spec.default_profile)
        return names

    def require_profile(self, name: str) -> None:
        if name not in self.profile_names():
            raise NotFoundError(
                f"Unknown environment profile '{name}'{did_you_mean(name, self.profile_names())}.",
                hint=f"Profiles: {', '.join(self.profile_names())} (see .highhx/environment.yaml).",
            )

    def is_protected(self, name: str) -> bool:
        return self.spec.profile(name).protected

    def switch_risk(self, name: str) -> RiskLevel:
        return RiskLevel.DANGEROUS if self.is_protected(name) else RiskLevel.SAFE

    def switch(self, name: str) -> None:
        self.require_profile(name)
        if self.state is None:
            raise ValidationError(
                "Cannot store the active profile outside an initialized project.", hint="Run `highhx init`."
            )
        self.state.set(ACTIVE_PROFILE_KEY, name)

    # ------------------------------------------------------------- reading
    def resolve(self, profile: str | None = None) -> ResolvedEnvironment:
        name = profile or self.active_profile()
        return resolve_profile(self.root, self.spec, name)

    def is_secret(self, name: str) -> bool:
        variable = self.spec.variables.get(name)
        return is_secret(name, variable.secret if variable else None)

    def secret_values(self, profile: str | None = None) -> list[str]:
        resolved = self.resolve(profile)
        return [v for k, v in resolved.values.items() if self.is_secret(k)]

    def describe(self, profile: str | None = None) -> list[dict[str, Any]]:
        """Variables with masked values, sources and declarations."""
        resolved = self.resolve(profile)
        names = sorted(set(resolved.values) | set(self.spec.variables))
        rows = []
        for name in names:
            variable = self.spec.variables.get(name)
            value = resolved.values.get(name)
            if value is None:
                value = os.environ.get(name)
                source = "shell environment" if value is not None else None
            else:
                source = resolved.sources.get(name)
            secret = self.is_secret(name)
            rows.append(
                {
                    "name": name,
                    "value": (mask(value) if secret else value) if value is not None else None,
                    "set": value is not None and value != "",
                    "secret": secret,
                    "required": bool(variable and variable.required_in(resolved.profile)),
                    "source": source,
                    "description": variable.description if variable else "",
                }
            )
        return rows

    def check(self, profile: str | None = None) -> list[CheckResult]:
        return check_environment(self.spec, self.resolve(profile))

    def diff(self, left: str, right: str) -> EnvDiff:
        for name in (left, right):
            self.require_profile(name)
        a, b = self.resolve(left).values, self.resolve(right).values
        diff = EnvDiff(left, right, sorted(set(a) - set(b)), sorted(set(b) - set(a)))
        for key in sorted(set(a) & set(b)):
            if a[key] == b[key]:
                diff.same += 1
            elif self.is_secret(key):
                diff.changed.append({"name": key, "left": mask(a[key]), "right": mask(b[key])})
            else:
                diff.changed.append({"name": key, "left": a[key], "right": b[key]})
        return diff

    # ------------------------------------------------------------- writing
    def target_file(self, profile: str, file: str | None = None) -> Path:
        if file:
            return self.root / file
        files = self.spec.profile(profile).files or [f".env.{profile}"]
        return self.root / files[0]

    def set(self, key: str, value: str, *, profile: str | None = None, file: str | None = None) -> tuple[Path, bool]:
        """Write ``key`` to the profile's .env file. Returns ``(path, existed)``."""
        if not is_env_name(key):
            raise ValidationError(
                f"Invalid variable name '{key}'.", hint="Use letters, digits and underscores; don't start with a digit."
            )
        name = profile or self.active_profile()
        variable = self.spec.variables.get(key)
        if variable is not None:
            import re

            if variable.pattern and not re.search(variable.pattern, value):
                raise ValidationError(f"Value for {key} does not match pattern {variable.pattern}.")
            if variable.choices and value not in variable.choices:
                raise ValidationError(f"Value for {key} must be one of: {', '.join(variable.choices)}.")
        path = self.target_file(name, file)
        existed = set_dotenv_value(path, key, value)
        self.protect_file(path)
        return path, existed

    def unset(self, key: str, *, profile: str | None = None, file: str | None = None) -> bool:
        return unset_dotenv_value(self.target_file(profile or self.active_profile(), file), key)

    def protect_file(self, path: Path) -> list[str]:
        """Ensure .env files are git-ignored. Returns entries added to .gitignore."""
        if (self.root / ".git").exists():
            rel = path.relative_to(self.root).as_posix() if path.is_relative_to(self.root) else path.name
            gitignore = self.root / ".gitignore"
            existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
            if rel not in existing.split() and ".env*" not in existing.split() and ".env.*" not in existing.split():
                return ensure_gitignore_entries(gitignore, [rel])
        return []

    def target_risk(self, profile: str) -> RiskLevel:
        return RiskLevel.DANGEROUS if self.is_protected(profile) else RiskLevel.NORMAL

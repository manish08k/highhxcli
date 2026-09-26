"""Declared environment variables (``.highhx/environment.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.config.defaults import DEFAULT_ENV_PROFILE
from highhx.config.loader import load_yaml
from highhx.core.errors import ConfigError
from highhx.utils.validation import Bool, List, Map, Obj, OneOf, Prop, Str, is_env_name, is_identifier

VARIABLE_SCHEMA = Obj(
    {
        "description": Prop(Str()),
        "required": Prop(Bool()),
        "secret": Prop(Bool()),
        "default": Prop(OneOf([Str(), Bool()])),
        "pattern": Prop(Str()),
        "choices": Prop(List(Str())),
        "profiles": Prop(List(Str()), description="Only required in these profiles"),
    }
)
PROFILE_SCHEMA = Obj(
    {
        "description": Prop(Str()),
        "files": Prop(List(Str())),
        "env": Prop(Map(Str(), key_check=is_env_name)),
        "protected": Prop(Bool()),
        "required": Prop(List(Str())),
    }
)
ENVIRONMENT_SCHEMA = Obj(
    {
        "default_profile": Prop(Str()),
        "required": Prop(List(Str())),
        "variables": Prop(Map(VARIABLE_SCHEMA, key_check=is_env_name, key_hint="invalid variable name")),
        "profiles": Prop(Map(PROFILE_SCHEMA, key_check=is_identifier, key_hint="invalid profile name")),
    }
)

DEFAULT_PROFILES: dict[str, dict[str, Any]] = {
    "development": {"files": [".env", ".env.development"]},
    "staging": {"files": [".env.staging"]},
    "production": {"files": [".env.production"], "protected": True},
}


@dataclass
class VariableSpec:
    name: str
    description: str = ""
    required: bool = False
    secret: bool | None = None
    default: str | None = None
    pattern: str | None = None
    choices: list[str] = field(default_factory=list)
    profiles: list[str] = field(default_factory=list)

    def required_in(self, profile: str) -> bool:
        return self.required and (not self.profiles or profile in self.profiles)


@dataclass
class ProfileSpec:
    name: str
    files: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    protected: bool = False
    required: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class EnvironmentSpec:
    default_profile: str = DEFAULT_ENV_PROFILE
    variables: dict[str, VariableSpec] = field(default_factory=dict)
    profiles: dict[str, ProfileSpec] = field(default_factory=dict)
    source: Path | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, source: Path | None = None) -> EnvironmentSpec:
        data = data or {}
        variables = {
            name: VariableSpec(
                name=name,
                description=spec.get("description") or "",
                required=bool(spec.get("required", False)),
                secret=spec.get("secret"),
                default=None
                if spec.get("default") is None
                else str(spec.get("default")).lower()
                if isinstance(spec.get("default"), bool)
                else str(spec.get("default")),
                pattern=spec.get("pattern"),
                choices=[str(c) for c in spec.get("choices") or []],
                profiles=[str(p) for p in spec.get("profiles") or []],
            )
            for name, spec in (data.get("variables") or {}).items()
        }
        for name in data.get("required") or []:
            variables.setdefault(name, VariableSpec(name=name)).required = True
        raw_profiles = data.get("profiles") or DEFAULT_PROFILES
        profiles = {
            name: ProfileSpec(
                name=name,
                files=[str(f) for f in (spec or {}).get("files") or [f".env.{name}"]],
                env={k: str(v) for k, v in ((spec or {}).get("env") or {}).items()},
                protected=bool((spec or {}).get("protected", False)),
                required=[str(r) for r in (spec or {}).get("required") or []],
                description=(spec or {}).get("description") or "",
            )
            for name, spec in raw_profiles.items()
        }
        return cls(
            default_profile=data.get("default_profile") or DEFAULT_ENV_PROFILE,
            variables=variables,
            profiles=profiles,
            source=source,
        )

    @classmethod
    def load(cls, path: Path) -> EnvironmentSpec:
        if not path.is_file():
            return cls.from_dict(None)
        data = load_yaml(path)
        errors = ENVIRONMENT_SCHEMA.validate(data, "") if data is not None else []
        if errors:
            raise ConfigError(f"Invalid {path.name}", details=errors, hint="Fix the listed fields.")
        return cls.from_dict(data, source=path)

    def profile(self, name: str) -> ProfileSpec:
        if name in self.profiles:
            return self.profiles[name]
        return ProfileSpec(name=name, files=[f".env.{name}"])

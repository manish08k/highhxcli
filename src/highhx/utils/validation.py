"""A tiny declarative schema validator.

HighhX validates configuration, workflows, plugin manifests and policies with
this module instead of pulling in a heavyweight dependency. Validators return a
list of human readable error strings prefixed with the offending path, so that
callers can report *all* problems at once.
"""

from __future__ import annotations

import abc
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DURATION_RE = re.compile(r"^\d+(\.\d+)?\s*(ms|s|m|h|d)?$", re.IGNORECASE)


def is_identifier(value: str) -> bool:
    """Step ids, task names, service names: letters, digits, ``_`` and ``-``."""
    return bool(IDENTIFIER_RE.match(value))


def is_env_name(value: str) -> bool:
    """Valid environment variable name."""
    return bool(ENV_NAME_RE.match(value))


def is_valid_port(value: object) -> bool:
    """TCP port in range 1-65535."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536


def is_url(value: str, schemes: Sequence[str] = ("http", "https")) -> bool:
    """Return True for an absolute URL with one of ``schemes``."""
    parsed = urlparse(value)
    return parsed.scheme in schemes and bool(parsed.netloc)


def did_you_mean(value: str, options: Sequence[str]) -> str:
    """Return a ``did you mean`` suffix for close matches."""
    import difflib

    close = difflib.get_close_matches(value, list(options), n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


class Schema(abc.ABC):
    """Base class for schema nodes."""

    description: str = ""

    @abc.abstractmethod
    def validate(self, value: Any, path: str) -> list[str]:
        """Return error messages for ``value`` located at ``path``."""

    @abc.abstractmethod
    def json_schema(self) -> dict[str, Any]:
        """Equivalent JSON Schema fragment."""


def _fmt(path: str) -> str:
    return path or "<root>"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "mapping"
    return type(value).__name__


@dataclass
class Any_(Schema):
    """Accept anything."""

    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        return []

    def json_schema(self) -> dict[str, Any]:
        return {}


@dataclass
class Str(Schema):
    """A string, optionally constrained by regex, choices or a custom check."""

    pattern: str | None = None
    choices: Sequence[str] | None = None
    min_length: int = 0
    check: Callable[[str], str | None] | None = None
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, str):
            return [f"{_fmt(path)}: expected a string, got {_type_name(value)}"]
        if len(value) < self.min_length:
            return [f"{_fmt(path)}: must not be empty"]
        if self.choices is not None and value not in self.choices:
            options = ", ".join(self.choices)
            return [f"{_fmt(path)}: '{value}' is not one of: {options}{did_you_mean(value, self.choices)}"]
        if self.pattern and not re.match(self.pattern, value):
            return [f"{_fmt(path)}: '{value}' does not match the required format"]
        if self.check:
            problem = self.check(value)
            if problem:
                return [f"{_fmt(path)}: {problem}"]
        return []

    def json_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "string"}
        if self.choices is not None:
            out["enum"] = list(self.choices)
        if self.pattern:
            out["pattern"] = self.pattern
        if self.min_length:
            out["minLength"] = self.min_length
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class Int(Schema):
    """An integer with optional bounds."""

    minimum: int | None = None
    maximum: int | None = None
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if isinstance(value, bool) or not isinstance(value, int):
            return [f"{_fmt(path)}: expected an integer, got {_type_name(value)}"]
        if self.minimum is not None and value < self.minimum:
            return [f"{_fmt(path)}: must be >= {self.minimum}"]
        if self.maximum is not None and value > self.maximum:
            return [f"{_fmt(path)}: must be <= {self.maximum}"]
        return []

    def json_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "integer"}
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class Num(Schema):
    """A number (int or float) with optional minimum."""

    minimum: float | None = None
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return [f"{_fmt(path)}: expected a number, got {_type_name(value)}"]
        if self.minimum is not None and value < self.minimum:
            return [f"{_fmt(path)}: must be >= {self.minimum}"]
        return []

    def json_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "number"}
        if self.minimum is not None:
            out["minimum"] = self.minimum
        return out


@dataclass
class Bool(Schema):
    """A boolean."""

    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, bool):
            return [f"{_fmt(path)}: expected true or false, got {_type_name(value)}"]
        return []

    def json_schema(self) -> dict[str, Any]:
        return {"type": "boolean"}


@dataclass
class Duration(Schema):
    """A duration: number of seconds or a string like ``30s``/``5m``."""

    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if isinstance(value, bool):
            return [f"{_fmt(path)}: expected a duration such as 30s or 5m"]
        if isinstance(value, int | float):
            return [] if value >= 0 else [f"{_fmt(path)}: duration must not be negative"]
        if isinstance(value, str) and DURATION_RE.match(value.strip()):
            return []
        return [f"{_fmt(path)}: invalid duration {value!r} (use e.g. 30, 30s, 5m, 1h)"]

    def json_schema(self) -> dict[str, Any]:
        return {
            "oneOf": [
                {"type": "number", "minimum": 0},
                {"type": "string", "pattern": DURATION_RE.pattern},
            ]
        }


@dataclass
class List(Schema):
    """A list whose items match ``item``."""

    item: Schema = field(default_factory=Any_)
    min_items: int = 0
    unique: bool = False
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, list):
            return [f"{_fmt(path)}: expected a list, got {_type_name(value)}"]
        errors: list[str] = []
        if len(value) < self.min_items:
            errors.append(f"{_fmt(path)}: must contain at least {self.min_items} item(s)")
        for index, item in enumerate(value):
            errors.extend(self.item.validate(item, f"{path}[{index}]"))
        if self.unique:
            seen: set[str] = set()
            for item in value:
                key = repr(item)
                if key in seen:
                    errors.append(f"{_fmt(path)}: duplicate entry {item!r}")
                seen.add(key)
        return errors

    def json_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "array", "items": self.item.json_schema()}
        if self.min_items:
            out["minItems"] = self.min_items
        return out


@dataclass
class Map(Schema):
    """A mapping with arbitrary (validated) keys and uniform values."""

    value: Schema = field(default_factory=Any_)
    key_check: Callable[[str], bool] | None = None
    key_hint: str = "invalid key"
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{_fmt(path)}: expected a mapping, got {_type_name(value)}"]
        errors: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                errors.append(f"{_fmt(path)}: keys must be strings, got {key!r}")
                continue
            if self.key_check and not self.key_check(key):
                errors.append(f"{_fmt(path)}.{key}: {self.key_hint}")
            errors.extend(self.value.validate(item, f"{path}.{key}" if path else key))
        return errors

    def json_schema(self) -> dict[str, Any]:
        return {"type": "object", "additionalProperties": self.value.json_schema()}


@dataclass
class Prop:
    """A property within an :class:`Obj`."""

    schema: Schema
    required: bool = False
    description: str = ""


@dataclass
class Obj(Schema):
    """A mapping with known properties. Unknown properties are errors unless ``extra``."""

    props: Mapping[str, Prop] = field(default_factory=dict)
    extra: bool = False
    description: str = ""
    check: Callable[[dict[str, Any]], list[str]] | None = None

    def validate(self, value: Any, path: str) -> list[str]:
        if not isinstance(value, dict):
            return [f"{_fmt(path)}: expected a mapping, got {_type_name(value)}"]
        errors: list[str] = []
        for name, prop in self.props.items():
            child = f"{path}.{name}" if path else name
            if name not in value or value[name] is None:
                if prop.required:
                    errors.append(f"{child}: is required")
                continue
            errors.extend(prop.schema.validate(value[name], child))
        if not self.extra:
            for key in value:
                if key not in self.props:
                    child = f"{path}.{key}" if path else str(key)
                    errors.append(f"{child}: unknown field{did_you_mean(str(key), list(self.props))}")
        if not errors and self.check:
            errors.extend(f"{_fmt(path)}: {msg}" for msg in self.check(value))
        return errors

    def json_schema(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "additionalProperties": self.extra,
        }
        required = []
        for name, prop in self.props.items():
            node = prop.schema.json_schema()
            if prop.description:
                node = {**node, "description": prop.description}
            out["properties"][name] = node
            if prop.required:
                required.append(name)
        if required:
            out["required"] = required
        if self.description:
            out["description"] = self.description
        return out


@dataclass
class OneOf(Schema):
    """Value must match at least one of the options (first match wins)."""

    options: Sequence[Schema] = ()
    description: str = ""

    def validate(self, value: Any, path: str) -> list[str]:
        collected: list[list[str]] = []
        for option in self.options:
            errors = option.validate(value, path)
            if not errors:
                return []
            collected.append(errors)
        # Prefer errors from an option whose *type* matched (they are more specific
        # than a bare "expected a string" style mismatch).
        type_mismatch = f"{_fmt(path)}: expected "
        specific = [errs for errs in collected if not errs[0].startswith(type_mismatch)]
        if specific:
            return specific[0]
        kinds = " or ".join(e[0].split("expected ", 1)[1].split(",")[0] for e in collected)
        return [f"{_fmt(path)}: expected {kinds}, got {_type_name(value)}"]

    def json_schema(self) -> dict[str, Any]:
        return {"oneOf": [option.json_schema() for option in self.options]}

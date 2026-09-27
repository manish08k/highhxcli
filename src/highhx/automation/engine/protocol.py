"""The automation-bridge protocol (version 1): the only things an automation engine may do.

HighhX talks to an automation engine — the C#/.NET engine (``engine/dotnet``) or the built-in
Python engine — with one JSON object per line:

    → {"v": 1, "id": 7, "op": "hotkey", "args": {"modifiers": ["cmd"], "key": "t"}}
    ← {"v": 1, "id": 7, "ok": true, "result": {"app": "Google Chrome"}}
    ← {"v": 1, "id": 7, "ok": false, "error": {"code": "accessibility_denied", "message": "…"}}

There is no "run this script" or "click at x,y" operation: every operation below is fixed,
every argument is validated here *before* anything is sent (and again by the engine), and
the executor has already classified and approved the action that asked for it. Keyboard
operations are refused when a terminal is frontmost — shell commands go through ``!command``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

PROTOCOL_VERSION = 1

MODIFIERS = {
    "cmd": "command",
    "command": "command",
    "ctrl": "control",
    "control": "control",
    "alt": "option",
    "option": "option",
    "opt": "option",
    "shift": "shift",
}
"""Modifier names people type → the protocol's canonical names."""

KEY_CODES = {
    "enter": 36,
    "return": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
    "arrowleft": 123,
    "arrowright": 124,
    "arrowdown": 125,
    "arrowup": 126,
    "pageup": 116,
    "pagedown": 121,
    "home": 115,
    "end": 119,
}
"""Named keys (macOS virtual key codes); any other key is one printable character."""

ROLES = frozenset({"button", "link", "textbox", "checkbox", "menuitem", "tab", "field", "any"})
SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
CHECKS = frozenset({"frontmost", "running", "window", "element"})
ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unknown_op",
        "unsupported_platform",
        "accessibility_denied",
        "not_found",
        "refused",
        "timeout",
        "failed",
    }
)
_APP_NAME = re.compile(r"^[\w .&+'()-]{1,100}$")


class ProtocolError(ValueError):
    """A request that the protocol does not allow (never sent to an engine)."""


# ------------------------------------------------------------------ arguments
@dataclass(frozen=True)
class Arg:
    kind: str
    """str, int, strs, url, app, key, modifiers, enum"""
    required: bool = False
    max_len: int = 200
    low: int = 0
    high: int = 0
    choices: frozenset[str] = frozenset()

    def check(self, name: str, value: Any) -> Any:
        if self.kind in ("str", "app", "key", "url", "enum"):
            if not isinstance(value, str):
                raise ProtocolError(f"{name} must be text")
            value = value.strip()
            if not value or len(value) > self.max_len:
                raise ProtocolError(f"{name} must be 1-{self.max_len} characters")
            if any(ord(c) < 32 and c not in "\t\n" for c in value):
                raise ProtocolError(f"{name} contains control characters")
        if self.kind == "app" and not _APP_NAME.match(value):
            raise ProtocolError(f"{name} is not an application name: {value!r}")
        if self.kind == "url":
            parsed = urlparse(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ProtocolError(f"{name} must be an http(s) URL")
        if self.kind == "key":
            value = value.lower()
            if value not in KEY_CODES and len(value) != 1:
                raise ProtocolError(f"unknown key {value!r} (named keys: {', '.join(sorted(KEY_CODES))})")
        if self.kind == "enum":
            value = value.lower()
            if value not in self.choices:
                raise ProtocolError(f"{name} must be one of {', '.join(sorted(self.choices))}")
        if self.kind == "int":
            if not isinstance(value, int) or isinstance(value, bool) or not self.low <= value <= self.high:
                raise ProtocolError(f"{name} must be a whole number from {self.low} to {self.high}")
        if self.kind == "modifiers":
            if not isinstance(value, list) or not 1 <= len(value) <= 3:
                raise ProtocolError(f"{name} must list 1 to 3 modifiers")
            try:
                value = sorted({MODIFIERS[str(m).lower()] for m in value})
            except KeyError as exc:
                raise ProtocolError(f"unknown modifier {exc.args[0]!r} (use cmd, ctrl, alt/option, shift)") from None
        return value


@dataclass(frozen=True)
class Op:
    name: str
    args: dict[str, Arg] = field(default_factory=dict)
    keyboard: bool = False
    """Sends input to the frontmost application (refused when that is a terminal)."""
    description: str = ""


OPS: dict[str, Op] = {
    op.name: op
    for op in (
        Op("status", description="Engine name, version, platform and whether Accessibility is granted."),
        Op("frontmost", description="The frontmost application and its focused window title."),
        Op("launch", {"app": Arg("app", required=True)}, description="Launch (or bring up) an application."),
        Op("focus", {"app": Arg("app", required=True)}, description="Bring a running application to the front."),
        Op("running", {"app": Arg("app", required=True)}, description="Whether an application is running."),
        Op(
            "open_url",
            {"url": Arg("url", required=True, max_len=2000), "app": Arg("app")},
            description="Open an http(s) URL in an application (default browser when none).",
        ),
        Op(
            "click",
            {"name": Arg("str", required=True), "role": Arg("enum", choices=ROLES), "app": Arg("app")},
            description="Press the UI element with this accessible name (and role) — never raw coordinates.",
        ),
        Op("type", {"text": Arg("str", required=True, max_len=2000)}, keyboard=True, description="Type text."),
        Op("key", {"key": Arg("key", required=True)}, keyboard=True, description="Press one key."),
        Op(
            "hotkey",
            {"modifiers": Arg("modifiers", required=True), "key": Arg("key", required=True)},
            keyboard=True,
            description="Press a key combination.",
        ),
        Op(
            "scroll",
            {"direction": Arg("enum", required=True, choices=SCROLL_DIRECTIONS), "amount": Arg("int", low=1, high=20)},
            description="Scroll the frontmost window.",
        ),
        Op("wait", {"ms": Arg("int", required=True, low=0, high=10_000)}, description="Wait (at most 10 s)."),
        Op(
            "inspect",
            {"app": Arg("app"), "limit": Arg("int", low=1, high=300)},
            description="Windows and UI elements (role, name, value) of an application.",
        ),
        Op(
            "verify",
            {
                "check": Arg("enum", required=True, choices=CHECKS),
                "app": Arg("app"),
                "name": Arg("str"),
                "role": Arg("enum", choices=ROLES),
            },
            description="Check UI state: frontmost / running application, a window, or an element.",
        ),
    )
}


def validate(op: str, args: dict[str, Any] | None) -> dict[str, Any]:
    """The request's arguments, normalised — or :class:`ProtocolError`."""
    spec = OPS.get(op)
    if spec is None:
        raise ProtocolError(f"unknown operation {op!r}")
    args = dict(args or {})
    extra = set(args) - set(spec.args)
    if extra:
        raise ProtocolError(f"{op} does not take {', '.join(sorted(extra))}")
    clean: dict[str, Any] = {}
    for name, arg in spec.args.items():
        if args.get(name) is None:
            if arg.required:
                raise ProtocolError(f"{op} needs {name}")
            continue
        clean[name] = arg.check(name, args[name])
    return clean


def request(op: str, args: dict[str, Any], request_id: int) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "id": request_id, "op": op, "args": validate(op, args)}


def describe() -> dict[str, Any]:
    """The protocol as data (``highhx computer engine --json``, the C# engine's tests)."""
    return {
        "version": PROTOCOL_VERSION,
        "ops": {
            op.name: {
                "description": op.description,
                "keyboard": op.keyboard,
                "args": {
                    name: {
                        "type": a.kind,
                        "required": a.required,
                        **({"choices": sorted(a.choices)} if a.choices else {}),
                    }
                    for name, a in op.args.items()
                },
            }
            for op in OPS.values()
        },
        "keys": sorted(KEY_CODES),
        "modifiers": sorted(set(MODIFIERS.values())),
        "errors": sorted(ERROR_CODES),
    }

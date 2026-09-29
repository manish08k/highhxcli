"""The automation-bridge protocol (version 1): the only things an automation engine may do.

HighhX talks to an automation engine — the C#/.NET engine (``engine/dotnet``) or the built-in
Python engine — with one JSON object per line:

    → {"v": 1, "id": 7, "op": "hotkey", "args": {"modifiers": ["cmd"], "key": "t"}}
    ← {"v": 1, "id": 7, "ok": true, "result": {"app": "Google Chrome"}}
    ← {"v": 1, "id": 7, "ok": false, "error": {"code": "accessibility_denied", "message": "…"}}

There is no "run this script" operation: every operation below is fixed, every argument is
validated here *before* anything is sent (and again by the engine), and the executor has
already classified and approved the action that asked for it. Keyboard operations are refused
when their target is a terminal — shell commands go through ``!command``.

Version 2 adds the HighhX Computer Runtime operations (observation — screen, screenshots,
applications, windows, the element at a point, the clipboard — and direct input — pointer
clicks at coordinates, drags, wheel scrolling, menus, window geometry, background delivery to
a named application). ``Op.since`` / ``Arg.since`` say which version introduced each; an
engine that speaks an older version is never sent them (:class:`~.bridge.AutomationBridge`
serves them from the built-in engine instead). Coordinates are desktop points (not pixels) in
the platform's global space, as ``screen`` reports it. This module is the contract: the JSON
export (``describe()`` → ``schemas/computer-protocol.json``) and the C# tables are checked
against it by tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

PROTOCOL_VERSION = 2
MIN_ENGINE_PROTOCOL = 1
"""The oldest engine protocol HighhX still talks to (its newer operations go to the built-in engine)."""

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
BUTTONS = frozenset({"left", "right", "middle"})
ERROR_CODES = frozenset(
    {
        "invalid_request",
        "unknown_op",
        "unsupported_platform",
        "unsupported",
        "accessibility_denied",
        "screen_recording_denied",
        "not_found",
        "refused",
        "timeout",
        "failed",
    }
)
COORDINATE_LIMIT = 100_000
"""Desktop points on any axis (negative on displays left of / above the main one)."""
FEATURES = (
    "accessibility",
    "screenshot",
    "window_screenshot",
    "pointer",
    "keyboard",
    "background_input",
    "windows",
    "window_frame",
    "applications",
    "menus",
    "clipboard",
    "element_at",
)
"""What ``capabilities`` reports, each with ``available`` and a ``detail`` saying why (not)."""
_APP_NAME = re.compile(r"^[\w .&+'()-]{1,100}$")


class ProtocolError(ValueError):
    """A request that the protocol does not allow (never sent to an engine)."""


# ------------------------------------------------------------------ arguments
@dataclass(frozen=True)
class Arg:
    kind: str
    """str, int, coord, strs, url, app, key, modifiers, enum, png"""
    required: bool = False
    max_len: int = 200
    low: int = 0
    high: int = 0
    choices: frozenset[str] = frozenset()
    since: int = 1

    def check(self, name: str, value: Any) -> Any:
        if self.kind == "coord":
            return Arg("int", low=-COORDINATE_LIMIT, high=COORDINATE_LIMIT).check(name, value)
        if self.kind == "strs":
            if not isinstance(value, list) or not 1 <= len(value) <= 8:
                raise ProtocolError(f"{name} must list 1 to 8 names")
            return [Arg("str", max_len=self.max_len).check(f"{name}[{i}]", v) for i, v in enumerate(value)]
        if self.kind == "png":
            return _png_path(name, value)
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
    """Sends input to the frontmost application — or to ``app`` — (refused when that is a terminal)."""
    description: str = ""
    since: int = 1

    def needs(self, args: dict[str, Any]) -> int:
        """The protocol version a request with these arguments needs."""
        return max([self.since, *(self.args[a].since for a in args if a in self.args)])


def _png_path(name: str, value: Any) -> str:
    """Where an engine may write a screenshot: a new ``.png`` in HighhX's screenshots folder only."""
    from pathlib import Path

    from highhx.utils.paths import user_data_dir

    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{name} must be a file path")
    path = Path(value)
    folder = (user_data_dir() / "screenshots").resolve()
    if not path.is_absolute() or path.suffix.lower() != ".png" or path.resolve().parent != folder:
        raise ProtocolError(f"{name} must be a .png file directly in {folder}")
    return str(path)


APP = Arg("app", since=2)
"""Deliver to this application in the background (without bringing it to the front)."""


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
        Op(
            "type",
            {"text": Arg("str", required=True, max_len=2000), "app": APP},
            keyboard=True,
            description="Type text.",
        ),
        Op("key", {"key": Arg("key", required=True), "app": APP}, keyboard=True, description="Press one key."),
        Op(
            "hotkey",
            {"modifiers": Arg("modifiers", required=True), "key": Arg("key", required=True), "app": APP},
            keyboard=True,
            description="Press a key combination.",
        ),
        Op(
            "scroll",
            {
                "direction": Arg("enum", required=True, choices=SCROLL_DIRECTIONS),
                "amount": Arg("int", low=1, high=20),
                "x": Arg("coord", since=2),
                "y": Arg("coord", since=2),
            },
            description="Scroll the frontmost window (version 2: with the mouse wheel, at a point when given).",
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
        # ------------------------------------------------ version 2: the HighhX Computer Runtime
        Op("capabilities", description="What this platform and engine can do, and why not.", since=2),
        Op("screen", description="The main display: size in points and the pixel scale factor.", since=2),
        Op(
            "screenshot",
            {"path": Arg("png", required=True), "window": Arg("int", low=0, high=2**31 - 1)},
            description="Capture the screen (or one window) to a PNG in HighhX's screenshots folder.",
            since=2,
        ),
        Op("apps", description="Running applications with a user interface (name, pid, frontmost).", since=2),
        Op(
            "windows",
            {"app": Arg("app")},
            description="Top-level windows front to back: id, application, pid, title, bounds.",
            since=2,
        ),
        Op(
            "window_frame",
            {
                "window": Arg("int", required=True, low=0, high=2**31 - 1),
                "x": Arg("coord", required=True),
                "y": Arg("coord", required=True),
                "width": Arg("int", required=True, low=1, high=COORDINATE_LIMIT),
                "height": Arg("int", required=True, low=1, high=COORDINATE_LIMIT),
            },
            description="Move and resize one window.",
            since=2,
        ),
        Op(
            "quit",
            {"app": Arg("app", required=True)},
            description="Ask an application to quit (it may ask to save).",
            since=2,
        ),
        Op(
            "click_at",
            {
                "x": Arg("coord", required=True),
                "y": Arg("coord", required=True),
                "button": Arg("enum", choices=BUTTONS),
                "count": Arg("int", low=1, high=3),
                "app": Arg("app"),
            },
            description="Click at a desktop point (right/middle, double/triple); to `app` in the background when given.",
            since=2,
        ),
        Op(
            "move",
            {"x": Arg("coord", required=True), "y": Arg("coord", required=True)},
            description="Move the pointer.",
            since=2,
        ),
        Op("cursor", description="Where the pointer is.", since=2),
        Op(
            "drag",
            {
                "from_x": Arg("coord", required=True),
                "from_y": Arg("coord", required=True),
                "to_x": Arg("coord", required=True),
                "to_y": Arg("coord", required=True),
                "button": Arg("enum", choices=BUTTONS),
                "duration_ms": Arg("int", low=0, high=5_000),
            },
            description="Press at one point, move to another, release.",
            since=2,
        ),
        Op(
            "element_at",
            {"x": Arg("coord", required=True), "y": Arg("coord", required=True)},
            description="The accessibility element at a point: application, role, name, bounds.",
            since=2,
        ),
        Op(
            "menu",
            {"app": Arg("app", required=True), "path": Arg("strs", required=True, max_len=100)},
            description='Choose an application menu item by its path, e.g. ["File", "Save"].',
            since=2,
        ),
        Op("clipboard_read", description="The clipboard's plain text.", since=2),
        Op(
            "clipboard_write",
            {"text": Arg("str", required=True, max_len=100_000)},
            description="Replace the clipboard with plain text.",
            since=2,
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


def request(op: str, args: dict[str, Any], request_id: int, *, version: int = PROTOCOL_VERSION) -> dict[str, Any]:
    return {"v": version, "id": request_id, "op": op, "args": validate(op, args)}


def describe() -> dict[str, Any]:
    """The protocol as data: ``highhx computer protocol`` and ``schemas/computer-protocol.json``."""
    return {
        "version": PROTOCOL_VERSION,
        "ops": {
            op.name: {
                "description": op.description,
                "since": op.since,
                "keyboard": op.keyboard,
                "args": {
                    name: {
                        "type": a.kind,
                        "required": a.required,
                        "since": max(a.since, op.since),
                        **({"choices": sorted(a.choices)} if a.choices else {}),
                    }
                    for name, a in op.args.items()
                },
            }
            for op in OPS.values()
        },
        "keys": sorted(KEY_CODES),
        "modifiers": sorted(set(MODIFIERS.values())),
        "buttons": sorted(BUTTONS),
        "features": list(FEATURES),
        "errors": sorted(ERROR_CODES),
    }

"""A vision model's answer → one GUI action, or a precise reason it is not one.

Two answer styles are understood:

UI-TARS (``Thought: … / Action: …``)::

    Thought: The Save button is at the top right.
    Action: click(start_box='(1012,64)')

    click / left_single / left_double / right_single / hover(start_box='(x,y)' or '(x1,y1,x2,y2)')
    drag(start_box='…', end_box='…') · scroll(start_box='…', direction='down') · type(content='…')
    hotkey(key='ctrl c') · press(key='enter') · wait() · finished(content='…') · call_user()
    open_app(app_name='…') · mouse_down / mouse_up(start_box='…')

JSON (any model that can follow instructions)::

    {"thought": "…", "action": "click", "x": 1012, "y": 64}

A box is reduced to its centre; ``<|box_start|>``/``<point>`` wrappers are accepted. Nothing in the
answer is executed here: the result is data — a :class:`GuiAction` from a closed set, with checked
fields — that the operator validates against the screenshot and runs through the action executor.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from highhx.core.errors import HighhXError

KINDS = frozenset(
    {
        "click",
        "double_click",
        "right_click",
        "hover",
        "mouse_down",
        "mouse_up",
        "drag",
        "scroll",
        "type",
        "key",
        "hotkey",
        "copy",
        "paste",
        "wait",
        "screenshot",
        "open_app",
        "focus_window",
        "close_window",
        "minimize_window",
        "maximize_window",
        "finished",
        "call_user",
    }
)
POINTED = frozenset({"click", "double_click", "right_click", "hover", "mouse_down", "mouse_up", "drag"})
ALIASES = {
    "left_single": "click",
    "left_click": "click",
    "left_double": "double_click",
    "double": "double_click",
    "right_single": "right_click",
    "mouse_move": "hover",
    "move": "hover",
    "press": "key",
    "keypress": "key",
    "key_press": "key",
    "write": "type",
    "finish": "finished",
    "done": "finished",
    "ask_user": "call_user",
    "launch": "open_app",
    "open": "open_app",
    "left_down": "mouse_down",
    "left_up": "mouse_up",
}
DIRECTIONS = frozenset({"up", "down", "left", "right"})
MAX_TEXT = 2000
MAX_WAIT = 10.0


class ActionParseError(HighhXError):
    """The answer is not one valid GUI action (the operator tells the model what was wrong)."""


@dataclass(frozen=True)
class GuiAction:
    kind: str
    point: tuple[float, float] | None = None
    end: tuple[float, float] | None = None
    text: str = ""
    keys: str = ""
    direction: str = ""
    seconds: float = 0.0
    app: str = ""
    window: int | None = None
    thought: str = ""
    raw: str = field(default="", compare=False)

    def describe(self) -> str:
        where = f" at {_fmt(self.point)}" if self.point else ""
        to = f" to {_fmt(self.end)}" if self.end else ""
        extra = {
            "type": f" {self.text[:40]!r}",
            "key": f" {self.keys}",
            "hotkey": f" {self.keys}",
            "scroll": f" {self.direction}",
            "open_app": f" {self.app}",
            "focus_window": f" {self.window}",
            "wait": f" {self.seconds:g}s",
            "finished": f": {self.text[:80]}" if self.text else "",
        }.get(self.kind, "")
        return f"{self.kind}{where}{to}{extra}"

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "", 0.0) and k != "raw"}


def _fmt(point: tuple[float, float] | None) -> str:
    return f"({point[0]:g}, {point[1]:g})" if point else ""


_NUMBER = r"-?\d+(?:\.\d+)?"
_CALL = re.compile(r"(?P<name>[a-z_]+)\s*\((?P<args>.*)\)\s*$", re.IGNORECASE | re.DOTALL)
_ARG = re.compile(r"(?P<key>\w+)\s*=\s*(?P<q>['\"])(?P<value>.*?)(?<!\\)(?P=q)", re.DOTALL)


def point_of(text: str) -> tuple[float, float]:
    """``'(x,y)'``, ``'(x1,y1,x2,y2)'`` (its centre), ``'<point>x y</point>'``, ``'[x, y]'``."""
    numbers = [float(n) for n in re.findall(_NUMBER, text)]
    if len(numbers) == 2:
        return numbers[0], numbers[1]
    if len(numbers) == 4:
        return (numbers[0] + numbers[2]) / 2, (numbers[1] + numbers[3]) / 2
    raise ActionParseError(f"{text!r} is not a point: give (x,y) or a box (x1,y1,x2,y2).")


def parse_action(answer: str) -> GuiAction:
    """The one action in a model's ``answer`` (see the module docstring)."""
    text = answer.strip()
    if not text:
        raise ActionParseError("The answer is empty; reply with one action.")
    data = _json_of(text)
    if data is not None:
        return _from_json(data, text)
    thought = ""
    match = re.search(r"Thought:\s*(?P<t>.*?)(?:\n\s*Action:|\Z)", text, re.DOTALL | re.IGNORECASE)
    if match:
        thought = match.group("t").strip()
    action = re.search(r"Action:\s*(?P<a>.+)", text, re.DOTALL | re.IGNORECASE)
    call = (action.group("a") if action else text).strip().splitlines()[0].strip().rstrip(";")
    return _from_call(call, thought, text)


def _json_of(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else (text if text.startswith("{") else None)
    if candidate is None:
        return None
    try:
        data = json.loads(candidate)
    except ValueError as exc:
        raise ActionParseError(f"The JSON answer does not parse: {exc}.") from None
    if not isinstance(data, dict):
        raise ActionParseError("The JSON answer must be an object.")
    return data


def _from_call(call: str, thought: str, raw: str) -> GuiAction:
    match = _CALL.match(call)
    if match is None:
        raise ActionParseError(f"No action call found in {call[:80]!r}; e.g. Action: click(start_box='(100,200)').")
    name = match.group("name").lower()
    args = {m.group("key").lower(): m.group("value").replace("\\'", "'").replace('\\"', '"') for m in _ARG.finditer(match.group("args"))}
    values: dict[str, Any] = {"action": name, "thought": thought}
    for key in ("start_box", "point", "start_point", "box"):
        if key in args:
            values["point"] = point_of(args[key])
            break
    for key in ("end_box", "end_point"):
        if key in args:
            values["end"] = point_of(args[key])
    values.update({k: v for k, v in args.items() if k in ("content", "key", "direction", "app_name", "window")})
    return _build(values, raw)


def _from_json(data: dict[str, Any], raw: str) -> GuiAction:
    values: dict[str, Any] = {"action": str(data.get("action") or data.get("type") or ""), "thought": str(data.get("thought") or "")}
    if data.get("x") is not None and data.get("y") is not None:
        values["point"] = (_num(data["x"], "x"), _num(data["y"], "y"))
    elif data.get("point") is not None:
        values["point"] = point_of(json.dumps(data["point"]))
    if data.get("to_x") is not None and data.get("to_y") is not None:
        values["end"] = (_num(data["to_x"], "to_x"), _num(data["to_y"], "to_y"))
    for key in ("text", "content", "keys", "key", "direction", "app", "app_name", "window", "seconds"):
        if data.get(key) is not None:
            values[key] = data[key]
    return _build(values, raw)


def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ActionParseError(f"{name} must be a number.")
    try:
        return float(value)
    except ValueError:
        raise ActionParseError(f"{name} must be a number, not {value!r}.") from None


def _build(values: dict[str, Any], raw: str) -> GuiAction:
    name = str(values.get("action") or "").lower().strip()
    kind = ALIASES.get(name, name)
    if kind not in KINDS:
        raise ActionParseError(f"Unknown action {name!r}; use one of: {', '.join(sorted(KINDS))}.")
    point, end = values.get("point"), values.get("end")
    if kind in POINTED and point is None:
        raise ActionParseError(f"{kind} needs a point in the screenshot.")
    if kind == "drag" and end is None:
        raise ActionParseError("drag needs an end point (end_box / to_x, to_y).")
    text = str(values.get("content") or values.get("text") or "")
    if len(text) > MAX_TEXT:
        raise ActionParseError(f"Text is limited to {MAX_TEXT} characters per action.")
    if kind == "type" and not text:
        raise ActionParseError("type needs the text (content).")
    keys = str(values.get("keys") or values.get("key") or "").strip().lower()
    if kind in ("key", "hotkey"):
        if not keys:
            raise ActionParseError(f"{kind} needs key(s), e.g. 'enter' or 'ctrl c'.")
        keys = "+".join(p for p in re.split(r"[\s+]+", keys) if p)
        kind = "hotkey" if "+" in keys else "key"
    direction = str(values.get("direction") or "").lower()
    if kind == "scroll":
        direction = direction or "down"
        if direction not in DIRECTIONS:
            raise ActionParseError(f"scroll direction must be one of {', '.join(sorted(DIRECTIONS))}.")
    app = str(values.get("app") or values.get("app_name") or "").strip()
    if kind == "open_app" and not app:
        raise ActionParseError("open_app needs the application's name.")
    window = values.get("window")
    if kind == "focus_window":
        try:
            window = int(window)
        except (TypeError, ValueError):
            raise ActionParseError("focus_window needs the window id (from the window list).") from None
    seconds = min(MAX_WAIT, max(0.0, _num(values.get("seconds") or 2, "seconds"))) if kind == "wait" else 0.0
    return GuiAction(
        kind,
        tuple(point) if point is not None else None,  # type: ignore[arg-type]
        tuple(end) if end is not None else None,  # type: ignore[arg-type]
        text,
        keys,
        direction,
        seconds,
        app,
        window if kind == "focus_window" else None,
        str(values.get("thought") or ""),
        raw,
    )

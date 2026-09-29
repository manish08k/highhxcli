"""The HighhX Task IR: a goal as validated, structured data.

    {
      "goal": "Open YouTube, search for a song and play the first result",
      "constraints": {"max_steps": 40, "timeout": 300},
      "context": {"start_url": "https://www.youtube.com"},
      "steps": [ {"action": "browser.navigate", "value": "https://www.youtube.com", "reason": "…"}, … ],
      "success_conditions": [ {"media_playing": true} ],
      "failure_conditions": [ {"text": "This video is unavailable"} ],
      "allow_replanning": true
    }

Steps are generic browser primitives (never site- or task-specific functions), plus two control
forms: ``{"if": condition, "then": [...], "else": [...]}`` and
``{"repeat": {"steps": [...], "until": condition, "max": n}}``. A task may have no steps at
all: then the planner discovers them from the page.

Every action — written by a person, derived by the deterministic resolver or proposed by a
model — is validated here against a closed schema: unknown fields, unknown primitives and
missing inputs are errors. There is no primitive that runs code or commands. Validation is
only the first gate; the executor then sends every action through the computer runtime's
safety policy, approval and audit (:mod:`highhx.computer.runtime`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from highhx.core.errors import ValidationError
from highhx.utils.validation import Bool, Int, List, Map, Num, Obj, Prop, Schema, Str

IR_VERSION = "1"

PRIMITIVES = (
    "navigate",
    "observe",
    "find",
    "click",
    "type",
    "read",
    "scroll",
    "select",
    "press",
    "wait",
    "new_tab",
    "close_tab",
    "switch_tab",
    "back",
    "forward",
    "upload",
    "download",
    "screenshot",
    "verify",
    "recover",
)
CONTROL = ("done", "fail", "ask_user")
"""Planner decisions that end the task (or hand it to the person); never executed on the page."""

ACTIONS = tuple(f"browser.{p}" for p in PRIMITIVES) + tuple(f"task.{c}" for c in CONTROL)

TARGETED = frozenset({"find", "click", "type", "select", "upload", "download", "switch_tab"})
NEEDS_VALUE = frozenset({"navigate", "type", "select", "press", "scroll", "upload"})
KEYS = ("enter", "tab", "escape")
"""Keys the runtime can press (its finite action set)."""
DIRECTIONS = ("up", "down")
IDEMPOTENT = frozenset(
    {"navigate", "observe", "find", "read", "scroll", "wait", "screenshot", "verify", "recover", "switch_tab"}
)
"""Primitives that may be issued again after a failure: repeating them cannot do anything twice.
Clicks, typing, key presses, selections, uploads, downloads and tab creation are never repeated
blindly — after a failure the page is observed again and the next action is decided anew."""

MAX_STEPS = 200
MAX_TIMEOUT = 3600.0
MAX_WAIT = 30.0
MAX_REPEAT = 25
MAX_ACTION_TIMEOUT = 120.0

DEFAULT_MAX_STEPS = 40
DEFAULT_TIMEOUT = 300.0
DEFAULT_MAX_FAILURES = 6
DEFAULT_ACTION_TIMEOUT = 15.0

CONDITION = Obj(
    {
        "url_contains": Prop(Str(min_length=1), description="The current URL contains this."),
        "title_contains": Prop(Str(min_length=1), description="The page title contains this (any case)."),
        "text": Prop(Str(min_length=1), description="The visible text contains this (any case)."),
        "text_matches": Prop(Str(min_length=1), description="A regular expression found in the visible text."),
        "element": Prop(Str(min_length=1), description="An element matching this target is on the page."),
        "absent": Prop(Str(min_length=1), description="No element matching this target is on the page."),
        "media_playing": Prop(Bool(), description="A video or audio element on the page is playing."),
        "download_completed": Prop(Bool(), description="The last download finished."),
    }
)

ACTION = Obj(
    {
        "action": Prop(Str(choices=ACTIONS), required=True, description="One generic primitive."),
        "target": Prop(
            Str(min_length=1),
            description='An element: an id from the last observation ("e12") or a selector — '
            '"button:Search", "textbox=\\"Email\\"", "link#2", "link[href*=/watch]#1".',
        ),
        "value": Prop(
            Str(),
            description="navigate/new_tab: URL · type: text · select: option · press: key · scroll: up|down · "
            "upload: file paths (one per line) · wait: seconds · read: a regular expression to extract · "
            "fail/ask_user: the reason or question.",
        ),
        "reason": Prop(Str(min_length=1), required=True, description="Why this action moves toward the goal."),
        "expected_result": Prop(Str(), description="What should be true afterwards, in words."),
        "expect": Prop(CONDITION, description="What should be true afterwards, checked."),
        "timeout": Prop(Num(minimum=0.0), description="Seconds to wait for the target / the expectation."),
    }
)


MAX_NESTING = 3
"""Branches and loops nest at most this deep (the schema is finite, and so is every plan)."""


@dataclass
class _StepSchema(Schema):
    """A step is an action, an ``if`` or a ``repeat`` — chosen by its key, so errors name the right one."""

    action: Schema
    branch: Schema
    loop: Schema

    def validate(self, value: Any, path: str) -> list[str]:
        if isinstance(value, dict):
            if "if" in value:
                return self.branch.validate(value, path)
            if "repeat" in value:
                return self.loop.validate(value, path)
        return self.action.validate(value, path)

    def json_schema(self) -> dict[str, Any]:
        return {"oneOf": [self.action.json_schema(), self.branch.json_schema(), self.loop.json_schema()]}


def _step_schema(depth: int = 0) -> Schema:
    if depth >= MAX_NESTING:
        return ACTION
    steps = List(_step_schema(depth + 1))
    branch = Obj(
        {
            "if": Prop(CONDITION, required=True),
            "then": Prop(steps, required=True),
            "else": Prop(steps),
            "reason": Prop(Str()),
        }
    )
    loop = Obj(
        {
            "repeat": Prop(
                Obj(
                    {
                        "steps": Prop(steps, required=True),
                        "until": Prop(CONDITION, required=True),
                        "max": Prop(Int(minimum=1, maximum=MAX_REPEAT), required=True),
                    }
                ),
                required=True,
            ),
            "reason": Prop(Str()),
        }
    )
    return _StepSchema(ACTION, branch, loop)


STEP = _step_schema()

TASK = Obj(
    {
        "version": Prop(Str(choices=(IR_VERSION,))),
        "goal": Prop(Str(min_length=1), required=True),
        "constraints": Prop(
            Obj(
                {
                    "max_steps": Prop(Int(minimum=1, maximum=MAX_STEPS)),
                    "timeout": Prop(Num(minimum=1.0)),
                    "max_failures": Prop(Int(minimum=0, maximum=50)),
                    "action_timeout": Prop(Num(minimum=0.0)),
                    "stay_on_site": Prop(Bool(), description="Never navigate away from the start site."),
                }
            )
        ),
        "context": Prop(Map(Str()), description="Facts for the planner (start_url, names …). Strings only."),
        "steps": Prop(List(STEP)),
        "success_conditions": Prop(List(CONDITION)),
        "failure_conditions": Prop(List(CONDITION)),
        "allow_replanning": Prop(Bool()),
    }
)


# ---------------------------------------------------------------------- model
@dataclass(frozen=True)
class Condition:
    url_contains: str | None = None
    title_contains: str | None = None
    text: str | None = None
    text_matches: str | None = None
    element: str | None = None
    absent: str | None = None
    media_playing: bool | None = None
    download_completed: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Condition:
        return cls(**{k: data[k] for k in CONDITION.props if k in data})

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    def describe(self) -> str:
        return (
            ", ".join(
                f"{k.replace('_', ' ')} {v!r}" if v is not True else k.replace("_", " ")
                for k, v in self.to_dict().items()
            )
            or "nothing"
        )

    @property
    def empty(self) -> bool:
        return not self.to_dict()


@dataclass(frozen=True)
class Action:
    action: str
    reason: str
    target: str | None = None
    value: str | None = None
    expected_result: str = ""
    expect: Condition | None = None
    timeout: float | None = None

    @property
    def primitive(self) -> str:
        return self.action.split(".", 1)[1]

    @property
    def control(self) -> bool:
        return self.action.startswith("task.")

    @property
    def idempotent(self) -> bool:
        return self.primitive in IDEMPOTENT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        errors = validate_action(data)
        if errors:
            raise ValidationError("Invalid action.", details=errors)
        return cls(
            str(data["action"]),
            str(data["reason"]),
            data.get("target"),
            data.get("value"),
            str(data.get("expected_result") or ""),
            Condition.from_dict(data["expect"]) if data.get("expect") else None,
            float(data["timeout"]) if data.get("timeout") is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"action": self.action}
        if self.target is not None:
            out["target"] = self.target
        if self.value is not None:
            out["value"] = self.value
        out["reason"] = self.reason
        if self.expected_result:
            out["expected_result"] = self.expected_result
        if self.expect is not None:
            out["expect"] = self.expect.to_dict()
        if self.timeout is not None:
            out["timeout"] = self.timeout
        return out

    def call(self) -> str:
        """``click(target='button:Search')`` — how the log shows it."""
        args = []
        if self.target is not None:
            args.append(f"target={self.target!r}")
        if self.value is not None:
            args.append(f"value={self.value!r}" if self.primitive != "type" or len(self.value) < 80 else "value=…")
        if self.expect is not None and self.primitive in ("verify", "wait"):
            args.append(self.expect.describe())
        return f"{self.primitive}({', '.join(args)})"

    def signature(self) -> str:
        return json.dumps(
            {k: v for k, v in self.to_dict().items() if k not in ("reason", "expected_result")}, sort_keys=True
        )


@dataclass(frozen=True)
class Branch:
    condition: Condition
    then: tuple[Step, ...]
    otherwise: tuple[Step, ...] = ()


@dataclass(frozen=True)
class Repeat:
    steps: tuple[Step, ...]
    until: Condition
    max: int


Step = Action | Branch | Repeat


@dataclass(frozen=True)
class Constraints:
    max_steps: int = DEFAULT_MAX_STEPS
    timeout: float = DEFAULT_TIMEOUT
    max_failures: int = DEFAULT_MAX_FAILURES
    action_timeout: float = DEFAULT_ACTION_TIMEOUT
    stay_on_site: bool = False


@dataclass(frozen=True)
class TaskIR:
    goal: str
    constraints: Constraints = field(default_factory=Constraints)
    context: dict[str, str] = field(default_factory=dict)
    steps: tuple[Step, ...] = ()
    success_conditions: tuple[Condition, ...] = ()
    failure_conditions: tuple[Condition, ...] = ()
    allow_replanning: bool = True

    @property
    def dynamic(self) -> bool:
        """No steps: the planner must discover them from the page."""
        return not self.steps

    def to_dict(self) -> dict[str, Any]:
        c = self.constraints
        return {
            "version": IR_VERSION,
            "goal": self.goal,
            "constraints": {
                "max_steps": c.max_steps,
                "timeout": c.timeout,
                "max_failures": c.max_failures,
                "action_timeout": c.action_timeout,
                "stay_on_site": c.stay_on_site,
            },
            "context": dict(self.context),
            "steps": [_step_to_dict(s) for s in self.steps],
            "success_conditions": [s.to_dict() for s in self.success_conditions],
            "failure_conditions": [s.to_dict() for s in self.failure_conditions],
            "allow_replanning": self.allow_replanning,
        }


def _step_to_dict(step: Step) -> dict[str, Any]:
    if isinstance(step, Action):
        return step.to_dict()
    if isinstance(step, Branch):
        out: dict[str, Any] = {"if": step.condition.to_dict(), "then": [_step_to_dict(s) for s in step.then]}
        if step.otherwise:
            out["else"] = [_step_to_dict(s) for s in step.otherwise]
        return out
    return {"repeat": {"steps": [_step_to_dict(s) for s in step.steps], "until": step.until.to_dict(), "max": step.max}}


# ------------------------------------------------------------------ validation
def _action_rules(data: dict[str, Any], path: str) -> list[str]:
    """What the schema cannot say: which inputs each primitive needs, and their values."""
    name = str(data.get("action") or "")
    if "." not in name:
        return []
    kind, primitive = name.split(".", 1)
    errors: list[str] = []
    target, value = data.get("target"), data.get("value")
    if kind == "task":
        if primitive in ("fail", "ask_user") and not value:
            errors.append(f"{path}.value: {name} needs the reason or question")
        return errors
    if primitive in TARGETED and not target:
        errors.append(f"{path}.target: {name} needs a target element")
    if primitive in NEEDS_VALUE and value is None:
        errors.append(f"{path}.value: {name} needs a value")
    if primitive == "navigate" and value is not None and not _web_address(str(value)):
        errors.append(f"{path}.value: {value!r} is not a web address (http or https)")
    if primitive == "new_tab" and value and not _web_address(str(value)):
        errors.append(f"{path}.value: {value!r} is not a web address (http or https)")
    if primitive == "press" and value is not None and str(value).lower() not in KEYS:
        errors.append(f"{path}.value: key must be one of {', '.join(KEYS)}")
    if primitive == "scroll" and value is not None and str(value).lower() not in DIRECTIONS:
        errors.append(f"{path}.value: direction must be up or down")
    if primitive == "wait":
        if value is None and not data.get("expect"):
            errors.append(f"{path}: browser.wait needs seconds (value) or a condition (expect)")
        if value is not None:
            try:
                seconds = float(value)
            except ValueError:
                errors.append(f"{path}.value: seconds must be a number")
            else:
                if not 0 <= seconds <= MAX_WAIT:
                    errors.append(f"{path}.value: wait at most {MAX_WAIT:.0f}s")
    if primitive == "verify" and not data.get("expect"):
        errors.append(f"{path}.expect: browser.verify needs a condition")
    if primitive == "read" and value:
        errors += _regex_error(str(value), f"{path}.value")
    timeout = data.get("timeout")
    if isinstance(timeout, int | float) and timeout > MAX_ACTION_TIMEOUT:
        errors.append(f"{path}.timeout: at most {MAX_ACTION_TIMEOUT:.0f}s")
    if target is not None:
        errors += _target_error(str(target), f"{path}.target")
    expect = data.get("expect")
    if isinstance(expect, dict):
        errors += _condition_rules(expect, f"{path}.expect")
    return errors


def _condition_rules(data: dict[str, Any], path: str) -> list[str]:
    errors = []
    if not data:
        errors.append(f"{path}: an empty condition checks nothing")
    if data.get("text_matches"):
        errors += _regex_error(str(data["text_matches"]), f"{path}.text_matches")
    for key in ("element", "absent"):
        if data.get(key):
            errors += _target_error(str(data[key]), f"{path}.{key}")
    return errors


def _regex_error(pattern: str, path: str) -> list[str]:
    try:
        re.compile(pattern)
    except re.error as exc:
        return [f"{path}: invalid regular expression ({exc})"]
    return []


def _target_error(target: str, path: str) -> list[str]:
    from highhx.goals.targets import parse_target

    try:
        parse_target(target)
    except ValueError as exc:
        return [f"{path}: {exc}"]
    return []


_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)


def _web_address(url: str) -> bool:
    """http(s) only: ``javascript:``, ``data:``, ``file:`` … are never addresses (``host:8080`` is)."""
    from urllib.parse import urlparse

    if "://" not in url and _SCHEME.match(url):
        return False
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not any(c.isspace() for c in url)


def validate_action(data: Any, path: str = "action") -> list[str]:
    if not isinstance(data, dict):
        return [f"{path}: expected an object"]
    errors = ACTION.validate(data, path)
    return errors or _action_rules(data, path)


def _walk_rules(steps: list[Any], path: str) -> list[str]:
    errors: list[str] = []
    for index, step in enumerate(steps):
        here = f"{path}[{index}]"
        if not isinstance(step, dict):
            continue
        if "action" in step:
            errors += _action_rules(step, here)
            if str(step.get("action", "")).startswith("task."):
                errors.append(f"{here}.action: {step['action']} is a planner decision, not a step")
        elif "if" in step:
            errors += _condition_rules(step["if"], f"{here}.if")
            errors += _walk_rules(step.get("then") or [], f"{here}.then")
            errors += _walk_rules(step.get("else") or [], f"{here}.else")
        elif "repeat" in step:
            body = step["repeat"]
            errors += _condition_rules(body.get("until") or {}, f"{here}.repeat.until")
            errors += _walk_rules(body.get("steps") or [], f"{here}.repeat.steps")
    return errors


def validate_task(data: Any) -> list[str]:
    """Every problem with a Task IR document (schema first, then per-primitive rules)."""
    if not isinstance(data, dict):
        return ["task: expected an object"]
    errors = TASK.validate(data, "task")
    if errors:
        return errors
    errors = _walk_rules(data.get("steps") or [], "task.steps")
    for key in ("success_conditions", "failure_conditions"):
        for index, cond in enumerate(data.get(key) or []):
            errors += _condition_rules(cond, f"task.{key}[{index}]")
    if not (data.get("steps") or data.get("success_conditions")):
        errors.append("task: give steps, success_conditions, or both — otherwise nothing says when it is done")
    constraints = data.get("constraints") or {}
    if isinstance(constraints.get("timeout"), int | float) and constraints["timeout"] > MAX_TIMEOUT:
        errors.append(f"task.constraints.timeout: at most {MAX_TIMEOUT:.0f}s")
    return errors


def _parse_step(data: dict[str, Any]) -> Step:
    if "action" in data:
        return Action.from_dict(data)
    if "if" in data:
        return Branch(
            Condition.from_dict(data["if"]),
            tuple(_parse_step(s) for s in data["then"]),
            tuple(_parse_step(s) for s in data.get("else") or []),
        )
    body = data["repeat"]
    return Repeat(tuple(_parse_step(s) for s in body["steps"]), Condition.from_dict(body["until"]), int(body["max"]))


def parse_task(data: Any) -> TaskIR:
    """A validated :class:`TaskIR`; raises :class:`ValidationError` listing every problem."""
    errors = validate_task(data)
    if errors:
        raise ValidationError("The task is not valid Task IR.", details=errors)
    c = data.get("constraints") or {}
    return TaskIR(
        goal=str(data["goal"]),
        constraints=Constraints(
            max_steps=int(c.get("max_steps", DEFAULT_MAX_STEPS)),
            timeout=float(c.get("timeout", DEFAULT_TIMEOUT)),
            max_failures=int(c.get("max_failures", DEFAULT_MAX_FAILURES)),
            action_timeout=float(c.get("action_timeout", DEFAULT_ACTION_TIMEOUT)),
            stay_on_site=bool(c.get("stay_on_site", False)),
        ),
        context={str(k): str(v) for k, v in (data.get("context") or {}).items()},
        steps=tuple(_parse_step(s) for s in data.get("steps") or []),
        success_conditions=tuple(Condition.from_dict(s) for s in data.get("success_conditions") or []),
        failure_conditions=tuple(Condition.from_dict(s) for s in data.get("failure_conditions") or []),
        allow_replanning=bool(data.get("allow_replanning", True)),
    )


def loads_json(text: str) -> Any:
    """JSON from a model or a file: a bare object, or one inside a ```json fence."""
    raw = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    elif not raw.startswith("{"):
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "Not valid JSON.", details=[f"{exc.msg} at line {exc.lineno} column {exc.colno}"]
        ) from None


def json_schemas(*, compact: bool = False) -> dict[str, Any]:
    """The JSON Schemas (``highhx computer task --schema``). ``compact``: the action and condition
    schemas only — what a planner prompt needs (the full task schema spells out nested steps)."""
    schemas = {"action": ACTION.json_schema(), "condition": CONDITION.json_schema()}
    return schemas if compact else {"task": TASK.json_schema(), **schemas}

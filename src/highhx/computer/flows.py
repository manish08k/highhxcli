"""Deterministic automation flows (HighhX Free): known targets, no AI.

```yaml
name: search-shop
steps:
  - open: http://127.0.0.1:8000
  - type: {into: "searchbox:Search", text: "Adele"}
  - press: enter
  - expect: {text: "You searched for Adele", url_contains: results}
  - click: "link:Next page"
  - select: {in: "combobox:Size", option: L}
  - type: {into: "textbox:Password", text_from_env: SHOP_PASSWORD}   # never logged
  - launch: Calculator
  - wait: 1.5
```

Every step is discovered (element resolved by role and accessible name), validated,
checked by the safety gate (sensitive steps ask for confirmation), executed and
verified. When a target is not there yet the step re-observes until ``timeout``
(recovery for slow pages); a step that still fails stops the flow.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from highhx.computer.model import Selector
from highhx.computer.runtime import ActionOutcome, ComputerRuntime, Expectation
from highhx.core.errors import NotFoundError, UsageError, ValidationError
from highhx.utils.validation import Any_, List, Map, Num, Obj, Prop, Str

STEP_KEYS = ("open", "click", "type", "press", "select", "scroll", "expect", "launch", "wait")

FLOW_SCHEMA = Obj(
    {
        "name": Prop(Str()),
        "description": Prop(Str()),
        "timeout": Prop(Num(minimum=0), description="Seconds to wait for targets (default 10)."),
        "steps": Prop(List(Map(Any_()), min_items=1), required=True),
    }
)
STEP_SCHEMAS = {
    "open": Str(min_length=1),
    "click": Str(min_length=1),
    "press": Str(
        choices=("enter", "tab", "escape", "backspace", "arrowdown", "arrowup", "pagedown", "pageup", "space")
    ),
    "scroll": Str(choices=("up", "down")),
    "launch": Str(min_length=1),
    "wait": Num(minimum=0),
    "type": Obj({"into": Prop(Str(min_length=1), required=True), "text": Prop(Str()), "text_from_env": Prop(Str())}),
    "select": Obj({"in": Prop(Str(min_length=1), required=True), "option": Prop(Str(), required=True)}),
    "expect": Obj(
        {
            "text": Prop(Str()),
            "url_contains": Prop(Str()),
            "title_contains": Prop(Str()),
            "element": Prop(Str()),
            "absent": Prop(Str()),
        }
    ),
}


@dataclass
class Flow:
    name: str
    steps: list[dict[str, Any]]
    timeout: float = 10.0
    source: Path | None = None


@dataclass
class FlowResult:
    name: str
    ok: bool
    steps: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "steps": self.steps}


def load_flow(path: Path) -> Flow:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValidationError(f"Cannot read flow {path}: {exc}") from None
    errors = FLOW_SCHEMA.validate(data, "")
    if not errors:
        for index, step in enumerate(data["steps"], start=1):
            keys = [k for k in step if k in STEP_KEYS]
            if len(keys) != 1 or len(step) != 1:
                errors.append(f"steps[{index}]: exactly one of {', '.join(STEP_KEYS)} is required")
                continue
            errors += STEP_SCHEMAS[keys[0]].validate(step[keys[0]], f"steps[{index}].{keys[0]}")
            if keys[0] == "type" and not ({"text", "text_from_env"} & set(step["type"])):
                errors.append(f"steps[{index}].type: needs 'text' or 'text_from_env'")
    if errors:
        raise ValidationError(f"Invalid flow {path.name}", details=errors)
    return Flow(str(data.get("name") or path.stem), list(data["steps"]), float(data.get("timeout", 10)), path)


def _describe(step: dict[str, Any]) -> str:
    key, value = next(iter(step.items()))
    if key == "type":
        shown = "$" + value["text_from_env"] if "text_from_env" in value else repr(value.get("text", ""))
        return f"type {shown} into {value['into']}"
    if key == "select":
        return f"select {value['option']!r} in {value['in']}"
    if key == "expect":
        return "expect " + ", ".join(f"{k}={v!r}" for k, v in value.items())
    return f"{key} {value}"


class FlowRunner:
    def __init__(self, runtime: ComputerRuntime, *, launch: Any = None, poll: float = 0.25) -> None:
        self.runtime = runtime
        self.launch = launch
        self.poll = poll

    def run(self, flow: Flow, *, on_step: Any = None) -> FlowResult:
        result = FlowResult(flow.name, True)
        for number, step in enumerate(flow.steps, start=1):
            label = _describe(step)
            try:
                outcome = self._step(step, flow.timeout)
            except (NotFoundError, UsageError, ValidationError) as exc:
                result.steps.append({"step": number, "action": label, "ok": False, "error": exc.message})
                result.ok = False
                if on_step:
                    on_step(number, label, False, exc.message)
                break
            entry = {"step": number, "action": label, "ok": outcome.ok, "verified": outcome.verified}
            if outcome.problems:
                entry["problems"] = outcome.problems
            result.steps.append(entry)
            if on_step:
                on_step(number, label, outcome.ok, "; ".join(outcome.problems))
            if not outcome.ok:
                result.ok = False
                break
        return result

    def _wait_for(self, selector: Selector, timeout: float) -> str:
        """Discover the target, re-observing until it appears (recovery for slow UIs)."""
        deadline = time.monotonic() + timeout
        while True:
            self.runtime.observe()
            try:
                return self.runtime.resolve(selector).id
            except NotFoundError:
                if time.monotonic() >= deadline:
                    raise
                if self.runtime.cancel.wait(self.poll):
                    raise

    def _step(self, step: dict[str, Any], timeout: float) -> ActionOutcome:
        key, value = next(iter(step.items()))
        rt = self.runtime
        if key == "open":
            return rt.navigate(str(value))
        if key == "launch":
            if self.launch is None:
                raise UsageError("This flow runner cannot launch applications.")
            return self.launch(str(value))
        if key == "wait":
            rt.cancel.wait(float(value))
            return ActionOutcome(f"wait {value}", True, None, f"waited {value}s")
        if key == "press":
            rt.observe()
            return rt.act(f"press:{value}")
        if key == "scroll":
            rt.observe()
            return rt.act(f"scroll:{value}")
        if key == "click":
            element = self._wait_for(Selector.parse(str(value)), timeout)
            return rt.act(f"click:{element}")
        if key == "type":
            text = value.get("text")
            if "text_from_env" in value:
                text = os.environ.get(str(value["text_from_env"]))
                if text is None:
                    raise UsageError(f"Environment variable {value['text_from_env']} is not set.")
                rt.gate.engine.redactor.add([text])
            element = self._wait_for(Selector.parse(str(value["into"])), timeout)
            return rt.act(f"type:{element}", str(text))
        if key == "select":
            element = self._wait_for(Selector.parse(str(value["in"])), timeout)
            return rt.act(f"select:{element}", str(value["option"]))
        if key == "expect":
            expectation = Expectation(
                text=value.get("text"),
                url_contains=value.get("url_contains"),
                title_contains=value.get("title_contains"),
                element=Selector.parse(value["element"]) if value.get("element") else None,
                absent=Selector.parse(value["absent"]) if value.get("absent") else None,
            )
            deadline = time.monotonic() + timeout
            while True:
                observation = rt.observe()
                problems = expectation.check(observation)
                if not problems or time.monotonic() >= deadline or rt.cancel.wait(self.poll):
                    return ActionOutcome("expect", not problems, not problems, "expectation", observation, problems)
        raise UsageError(f"Unknown step {key!r}")

"""Workflow YAML schema and typed models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RISK_NAMES, RiskLevel
from highhx.execution.retry import RetryPolicy
from highhx.utils.validation import (
    Any_,
    Bool,
    Duration,
    Int,
    List,
    Map,
    Num,
    Obj,
    OneOf,
    Prop,
    Str,
    is_env_name,
    is_identifier,
)


def _identifier(value: str) -> str | None:
    return None if is_identifier(value) else "must contain only letters, digits, '_' and '-' and start with a letter"


ENV_MAP = Map(OneOf([Str(), Num(), Bool()]), key_check=is_env_name, key_hint="invalid environment variable name")
SCALAR = OneOf([Str(), Num(), Bool()])

RETRY_SCHEMA = OneOf(
    [
        Int(minimum=1, maximum=100, description="Total attempts"),
        Obj(
            {
                "attempts": Prop(Int(minimum=1, maximum=100), required=True),
                "delay": Prop(Duration()),
                "backoff": Prop(Num(minimum=1)),
                "max_delay": Prop(Duration()),
                "retry_on_timeout": Prop(Bool()),
            }
        ),
    ]
)

APPROVAL_SCHEMA = OneOf(
    [
        Bool(),
        Obj(
            {
                "message": Prop(Str()),
                "risk": Prop(Str(choices=RISK_NAMES)),
                "bypassable": Prop(Bool()),
            }
        ),
    ]
)


def _step_check(step: dict[str, Any]) -> list[str]:
    has_run, has_uses = "run" in step, "uses" in step
    if has_run == has_uses:
        return ["a step needs exactly one of 'run' or 'uses'"]
    if "with" in step and not has_uses:
        return ["'with' is only valid together with 'uses'"]
    return []


STEP_SCHEMA = Obj(
    {
        "id": Prop(Str(check=_identifier), required=True, description="Unique step identifier"),
        "name": Prop(Str(), description="Display name"),
        "description": Prop(Str()),
        "run": Prop(OneOf([Str(min_length=1), List(Str(min_length=1), min_items=1)]), description="Command(s) to run"),
        "uses": Prop(Str(min_length=1), description="Name of another workflow to run as this step"),
        "with": Prop(Map(SCALAR), description="Inputs for the workflow referenced by 'uses'"),
        "depends_on": Prop(OneOf([Str(check=_identifier), List(Str(check=_identifier), unique=True)])),
        "if": Prop(OneOf([Str(min_length=1), Bool()]), description="Condition expression (or true/false)"),
        "env": Prop(ENV_MAP),
        "cwd": Prop(Str(min_length=1)),
        "timeout": Prop(Duration()),
        "retry": Prop(RETRY_SCHEMA),
        "approval": Prop(APPROVAL_SCHEMA),
        "continue_on_error": Prop(Bool()),
        "shell": Prop(Bool(), description="Force (true) or forbid (false) running through a shell"),
    },
    check=_step_check,
)

INPUT_SCHEMA = Obj(
    {
        "description": Prop(Str()),
        "default": Prop(SCALAR),
        "required": Prop(Bool()),
    }
)

SETTINGS_SCHEMA = Obj(
    {
        "fail_fast": Prop(Bool()),
        "max_parallel": Prop(Int(minimum=1, maximum=256)),
        "timeout": Prop(Duration()),
        "working_directory": Prop(Str(min_length=1)),
    }
)

WORKFLOW_SCHEMA = Obj(
    {
        "name": Prop(Str(min_length=1), required=True),
        "description": Prop(Str()),
        "version": Prop(Int(minimum=1, maximum=1)),
        "on": Prop(
            OneOf([Str(min_length=1), List(Str(min_length=1))]), description="Events that trigger this workflow"
        ),
        "inputs": Prop(Map(INPUT_SCHEMA, key_check=is_identifier, key_hint="invalid input name")),
        "env": Prop(ENV_MAP),
        "vars": Prop(Map(Any_())),
        "settings": Prop(SETTINGS_SCHEMA),
        "steps": Prop(List(STEP_SCHEMA, min_items=1), required=True),
        "outputs": Prop(Map(Str())),
    }
)


def workflow_json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for editor integration."""
    schema = WORKFLOW_SCHEMA.json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "HighhX workflow"
    return schema


@dataclass
class ApprovalSpec:
    message: str | None = None
    risk: RiskLevel = RiskLevel.DANGEROUS
    bypassable: bool = True


@dataclass
class InputSpec:
    name: str
    description: str = ""
    default: Any = None
    required: bool = False


@dataclass
class StepSpec:
    id: str
    name: str | None = None
    description: str = ""
    run: list[str] = field(default_factory=list)
    uses: str | None = None
    with_: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    condition: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    timeout: float | None = None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    approval: ApprovalSpec | None = None
    continue_on_error: bool = False
    shell: bool | None = None

    @property
    def label(self) -> str:
        return self.name or self.id


@dataclass
class WorkflowSettings:
    fail_fast: bool = True
    max_parallel: int = 4
    timeout: float | None = None
    working_directory: str | None = None


@dataclass
class WorkflowSpec:
    name: str
    steps: list[StepSpec]
    description: str = ""
    env: dict[str, str] = field(default_factory=dict)
    vars: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, InputSpec] = field(default_factory=dict)
    settings: WorkflowSettings = field(default_factory=WorkflowSettings)
    outputs: dict[str, str] = field(default_factory=dict)
    triggers: list[str] = field(default_factory=list)
    source: Path | None = None
    key: str | None = None
    """Identifier used on the command line (file stem)."""

    def step(self, step_id: str) -> StepSpec:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    @property
    def step_ids(self) -> list[str]:
        return [s.id for s in self.steps]

"""Parsing workflow documents into :class:`WorkflowSpec` objects."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel
from highhx.core.errors import WorkflowError
from highhx.execution.retry import RetryPolicy
from highhx.utils.time import parse_duration
from highhx.workflows.schema import (
    WORKFLOW_SCHEMA,
    ApprovalSpec,
    InputSpec,
    RollbackSpec,
    StepSpec,
    WorkflowSettings,
    WorkflowSpec,
)


def _str_map(values: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in (values or {}).items():
        if isinstance(value, bool):
            result[key] = "true" if value else "false"
        elif value is None:
            result[key] = ""
        else:
            result[key] = str(value)
    return result


def normalize_document(data: Any) -> Any:
    """Undo YAML 1.1 quirks: an unquoted ``on:`` key is parsed as boolean True."""
    if isinstance(data, dict) and True in data and "on" not in data:
        data["on"] = data.pop(True)
    return data


def schema_errors(data: Any) -> list[str]:
    """Structural errors only (see :mod:`highhx.workflows.validator` for semantic checks)."""
    data = normalize_document(data)
    if not isinstance(data, dict):
        return ["workflow must be a mapping with 'name' and 'steps'"]
    return WORKFLOW_SCHEMA.validate(data, "")


def _approval(value: Any) -> ApprovalSpec | None:
    if value is None or value is False:
        return None
    if value is True:
        return ApprovalSpec()
    return ApprovalSpec(
        message=value.get("message"),
        risk=RiskLevel.parse(value.get("risk", "dangerous")),
        bypassable=bool(value.get("bypassable", True)),
    )


def _condition(value: Any) -> str | None:
    """``if: false`` is parsed by YAML as a boolean; keep it as an expression."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return None if value is None else str(value)


def _rollback(value: Any) -> RollbackSpec | None:
    if value is None or value is False:
        return None
    if value is True:
        return RollbackSpec(compensate=True)
    if isinstance(value, str):
        return RollbackSpec(run=[value])
    if isinstance(value, list):
        return RollbackSpec(run=[str(v) for v in value])
    return RollbackSpec(action=str(value["action"]), with_=dict(value.get("with") or {}))


def _step(data: dict[str, Any]) -> StepSpec:
    run = data.get("run")
    depends = data.get("depends_on") or []
    return StepSpec(
        id=data["id"],
        name=data.get("name"),
        description=data.get("description") or "",
        run=[run] if isinstance(run, str) else list(run or []),
        uses=data.get("uses"),
        action=data.get("action"),
        with_=dict(data.get("with") or {}),
        rollback=_rollback(data.get("rollback")),
        depends_on=[depends] if isinstance(depends, str) else list(depends),
        condition=_condition(data.get("if")),
        env=_str_map(data.get("env")),
        cwd=data.get("cwd"),
        timeout=parse_duration(data.get("timeout")),
        retry=RetryPolicy.from_value(data.get("retry")),
        approval=_approval(data.get("approval")),
        continue_on_error=bool(data.get("continue_on_error", False)),
        shell=data.get("shell"),  # nosec B604 - parses the user's workflow `shell` setting; no execution here
        for_each=data.get("for_each"),
        verify=dict(data["verify"]) if isinstance(data.get("verify"), dict) else None,
        while_=str(data["while"]) if data.get("while") is not None else None,
        max_iterations=int(data.get("max_iterations") or 100),
        choose=[dict(b) for b in data["choose"]] if isinstance(data.get("choose"), list) else None,
        wait=_wait(data.get("wait")),
        handoff=str(data["handoff"]) if data.get("handoff") is not None else None,
        set_=dict(data["set"]) if isinstance(data.get("set"), dict) else None,
    )


def _wait(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            "until": str(value["until"]),
            "interval": parse_duration(value.get("interval")) or 1.0,
            "timeout": parse_duration(value.get("timeout")) or 300.0,
        }
    return parse_duration(value)


def branch_step(step_id: str, branch: dict[str, Any]) -> StepSpec:
    """A choose branch as a step body (its action/run/uses, with, verify and env)."""
    body = {k: v for k, v in branch.items() if k not in ("if", "elif", "else")}
    return _step({**body, "id": step_id})


def parse_workflow(data: Any, *, source: Path | None = None, key: str | None = None) -> WorkflowSpec:
    """Validate the structure of ``data`` and build a :class:`WorkflowSpec`.

    Raises :class:`WorkflowError` listing every structural problem.
    """
    data = normalize_document(data)
    errors = schema_errors(data)
    if errors:
        where = f" in {source.name}" if source else ""
        raise WorkflowError(
            f"Invalid workflow{where}", details=errors, hint="Run `highhx workflow validate` for details."
        )
    settings = data.get("settings") or {}
    triggers = data.get("on") or []
    return WorkflowSpec(
        name=data["name"],
        description=data.get("description") or "",
        steps=[_step(s) for s in data["steps"]],
        env=_str_map(data.get("env")),
        vars=dict(data.get("vars") or {}),
        inputs={
            name: InputSpec(
                name=name,
                description=spec.get("description") or "",
                default=spec.get("default"),
                required=bool(spec.get("required", False)),
            )
            for name, spec in (data.get("inputs") or {}).items()
        },
        settings=WorkflowSettings(
            fail_fast=bool(settings.get("fail_fast", True)),
            max_parallel=int(settings.get("max_parallel", 4)),
            timeout=parse_duration(settings.get("timeout")),
            working_directory=settings.get("working_directory"),
        ),
        outputs={k: str(v) for k, v in (data.get("outputs") or {}).items()},
        triggers=[triggers] if isinstance(triggers, str) else list(triggers),
        on_failure=str(data.get("on_failure") or "stop"),
        source=source,
        key=key or (source.stem if source else data["name"]),
    )

"""Resolution of inputs and reusable (``uses:``) workflows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from highhx.core.errors import NotFoundError, ValidationError, WorkflowError
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.schema import WorkflowSpec


def resolve_inputs(spec: WorkflowSpec, provided: Mapping[str, Any]) -> dict[str, Any]:
    """Merge defaults with provided inputs; error on unknown or missing required inputs."""
    errors: list[str] = []
    unknown = sorted(set(provided) - set(spec.inputs))
    errors.extend(f"unknown input '{name}'" for name in unknown)
    resolved: dict[str, Any] = {}
    for name, definition in spec.inputs.items():
        if name in provided:
            resolved[name] = provided[name]
        elif definition.default is not None:
            resolved[name] = definition.default
        elif definition.required:
            errors.append(f"missing required input '{name}'")
        else:
            resolved[name] = ""
    if errors:
        raise ValidationError(
            f"Invalid inputs for workflow '{spec.name}'",
            details=errors,
            hint="Pass inputs with --input name=value.",
        )
    return resolved


def reusable_workflows(spec: WorkflowSpec, loader: WorkflowLoader) -> dict[str, WorkflowSpec]:
    """Load the workflows referenced by ``uses:`` steps and reject recursive references."""
    resolved: dict[str, WorkflowSpec] = {}
    chain = [spec.key or spec.name]

    def _visit(current: WorkflowSpec) -> None:
        for step in current.steps:
            if not step.uses:
                continue
            if step.uses in chain:
                cycle = " -> ".join([*chain, step.uses])
                raise WorkflowError(f"Recursive workflow reference: {cycle}")
            try:
                child = loader.load(step.uses)
            except NotFoundError as exc:
                raise WorkflowError(f"Step '{step.id}' uses unknown workflow '{step.uses}'", hint=exc.hint) from exc
            resolved.setdefault(step.uses, child)
            chain.append(step.uses)
            _visit(child)
            chain.pop()

    _visit(spec)
    return resolved

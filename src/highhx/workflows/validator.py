"""Semantic workflow validation (run before any execution)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RiskLevel, classify_command
from highhx.core.errors import ConfigError, WorkflowError
from highhx.execution.command import CommandSpec, split_command
from highhx.utils.processes import which
from highhx.utils.validation import did_you_mean
from highhx.workflows import conditions
from highhx.workflows.dependency_graph import DependencyGraph
from highhx.workflows.loader import WorkflowLoader
from highhx.workflows.parser import normalize_document, parse_workflow, schema_errors
from highhx.workflows.resolver import reusable_workflows
from highhx.workflows.schema import StepSpec, WorkflowSpec
from highhx.workflows.variables import find_expressions

SHELL_BUILTINS = frozenset(
    {
        "cd",
        "echo",
        "exit",
        "export",
        "set",
        "true",
        "false",
        "test",
        "[",
        "source",
        ".",
        "exec",
        "eval",
        "type",
        "if",
        "for",
        "while",
    }
)


@dataclass
class ValidationReport:
    """Errors block execution; warnings are advisory."""

    workflow: str
    source: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow": self.workflow,
            "source": self.source,
            "valid": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def _duplicate_ids(data: dict[str, Any]) -> list[str]:
    ids = [s.get("id") for s in data.get("steps") or [] if isinstance(s, dict)]
    return sorted({i for i in ids if i is not None and ids.count(i) > 1})


def _check_expression(
    expr: str, where: str, spec: WorkflowSpec, step_id: str | None, graph: DependencyGraph, report: ValidationReport
) -> None:
    try:
        refs = conditions.references(expr)
    except conditions.ExpressionError as exc:
        report.errors.append(f"{where}: invalid expression '{expr}': {exc}")
        return
    ancestors = set(graph.ancestors(step_id)) if step_id and step_id in graph else set(spec.step_ids)
    for parts in refs:
        root = parts[0]
        if root not in conditions.KNOWN_ROOTS:
            report.errors.append(
                f"{where}: unknown context '{root}'{did_you_mean(root, sorted(conditions.KNOWN_ROOTS))}"
            )
            continue
        if root == "inputs" and len(parts) > 1 and parts[1] not in spec.inputs:
            report.errors.append(f"{where}: unknown input '{parts[1]}'")
        if root == "vars" and len(parts) > 1 and parts[1] not in spec.vars:
            report.errors.append(f"{where}: unknown variable '{parts[1]}'")
        if root == "steps":
            if len(parts) < 3:
                report.errors.append(f"{where}: incomplete step reference '{'.'.join(parts)}'")
                continue
            ref = parts[1]
            if ref not in spec.step_ids:
                report.errors.append(f"{where}: reference to unknown step '{ref}'{did_you_mean(ref, spec.step_ids)}")
            elif step_id is not None and ref not in ancestors:
                report.errors.append(
                    f"{where}: step '{ref}' is not a dependency of '{step_id}', so its results are not available"
                )
            if parts[2] not in ("outputs", "status", "exit_code"):
                report.errors.append(
                    f"{where}: steps.{ref}.{parts[2]} is not available (use outputs, status or exit_code)"
                )


def _check_command(
    command: str, where: str, report: ValidationReport, *, check_tools: bool, needs_approval: bool
) -> None:
    text = command.strip()
    if not text:
        report.errors.append(f"{where}: empty command")
        return
    if "${{" in text and "}}" not in text:
        report.errors.append(f"{where}: unterminated '${{{{' expression")
    spec = CommandSpec(text)
    if not spec.uses_shell():
        try:
            split_command(text)
        except ValueError as exc:
            report.errors.append(f"{where}: cannot parse command ({exc})")
            return
    program = spec.program()
    if check_tools and program and "${{" not in program and program not in SHELL_BUILTINS and program != "highhx":
        if not ("/" in program or "\\" in program) and which(program) is None:
            report.warnings.append(f"{where}: '{program}' was not found on PATH")
    classification = classify_command(text)
    if classification.risk >= RiskLevel.DANGEROUS and not needs_approval:
        reasons = "; ".join(classification.reasons)
        report.warnings.append(
            f"{where}: {classification.risk.label} command ({reasons}) — consider adding 'approval: true'"
        )


def validate_spec(
    spec: WorkflowSpec,
    *,
    loader: WorkflowLoader | None = None,
    check_tools: bool = True,
    base_dir: Path | None = None,
    report: ValidationReport | None = None,
) -> ValidationReport:
    """Semantic checks on a structurally valid workflow."""
    report = report or ValidationReport(workflow=spec.name, source=str(spec.source) if spec.source else None)
    graph = DependencyGraph()
    for step in spec.steps:
        graph.add_node(step.id)
    for step in spec.steps:
        for dep in step.depends_on:
            if dep == step.id:
                report.errors.append(f"steps.{step.id}.depends_on: a step cannot depend on itself")
                continue
            graph.add_edge(step.id, dep)
    for node, dep in graph.missing():
        report.errors.append(f"steps.{node}.depends_on: unknown step '{dep}'{did_you_mean(dep, spec.step_ids)}")
    cycle = graph.find_cycle()
    if cycle:
        report.errors.append(f"circular dependency: {' -> '.join(cycle)}")
        return report

    never_runs: set[str] = set()
    for step in spec.steps:
        where = f"steps.{step.id}"
        if step.condition:
            _check_expression(step.condition, f"{where}.if", spec, step.id, graph, report)
            try:
                is_const, value = conditions.constant_value(step.condition)
                if is_const and not value:
                    never_runs.add(step.id)
                    report.warnings.append(f"{where}.if: condition is always false; the step never runs")
            except conditions.ExpressionError:
                pass
        for index, command in enumerate(step.run):
            label = f"{where}.run" + (f"[{index}]" if len(step.run) > 1 else "")
            for expr in find_expressions(command):
                _check_expression(expr, label, spec, step.id, graph, report)
                if "steps." in expr and ".outputs" in expr:
                    report.warnings.append(
                        f"{label}: '${{{{ {expr} }}}}' inserts runtime output into the command line; "
                        "pass it through `env:` and use the variable instead to avoid shell injection"
                    )
            _check_command(command, label, report, check_tools=check_tools, needs_approval=step.approval is not None)
        for key, value in {**step.env, **{f"with.{k}": str(v) for k, v in step.with_.items()}}.items():
            for expr in find_expressions(value):
                _check_expression(
                    expr,
                    f"{where}.{key}" if key.startswith("with.") else f"{where}.env.{key}",
                    spec,
                    step.id,
                    graph,
                    report,
                )
        _check_action_step(step, where, report, check_tools=check_tools)
        if step.cwd and base_dir is not None and "${{" not in step.cwd and not (base_dir / step.cwd).is_dir():
            report.warnings.append(f"{where}.cwd: directory '{step.cwd}' does not exist")
        if step.timeout is not None and step.timeout == 0:
            report.warnings.append(f"{where}.timeout: 0 disables the timeout")

    for step in spec.steps:
        blocked = [a for a in graph.ancestors(step.id) if a in never_runs]
        if blocked and not (
            step.condition and conditions.uses_status_function(step.condition) and "always" in step.condition
        ):
            report.errors.append(
                f"steps.{step.id}: can never run because it depends on '{blocked[0]}', whose condition is always false"
            )

    if spec.on_failure == "rollback" and not any(step.rollback for step in spec.steps):
        report.warnings.append("on_failure: rollback, but no step defines `rollback:` — nothing would be undone")

    for key, value in spec.env.items():
        for expr in find_expressions(value):
            _check_expression(expr, f"env.{key}", spec, None, graph, report)
    for key, value in spec.outputs.items():
        for expr in find_expressions(value):
            _check_expression(expr, f"outputs.{key}", spec, None, graph, report)

    if loader is not None and any(s.uses for s in spec.steps):
        try:
            children = reusable_workflows(spec, loader)
        except WorkflowError as exc:
            report.errors.append(exc.message)
        else:
            for step in spec.steps:
                if step.uses and step.uses in children:
                    child = children[step.uses]
                    missing = [
                        n for n, i in child.inputs.items() if i.required and i.default is None and n not in step.with_
                    ]
                    unknown = [n for n in step.with_ if n not in child.inputs]
                    report.errors.extend(
                        f"steps.{step.id}.with: missing required input '{n}' for '{step.uses}'" for n in missing
                    )
                    report.errors.extend(f"steps.{step.id}.with: '{step.uses}' has no input '{n}'" for n in unknown)
    return report


def _static_inputs(values: dict[str, Any]) -> dict[str, Any] | None:
    """Inputs without ``${{ }}`` expressions (those can be checked before the run), else None."""
    if any("${{" in str(v) for v in values.values()):
        return None
    return dict(values)


def _check_action_step(step: StepSpec, where: str, report: ValidationReport, *, check_tools: bool) -> None:
    from highhx.actions.catalog import default_catalog

    catalog = default_catalog()
    if step.action:
        spec = catalog.get(step.action)
        if spec is None:
            names = catalog.names()
            report.errors.append(f"{where}.action: unknown action '{step.action}'{did_you_mean(step.action, names)}")
        else:
            inputs = _static_inputs(step.with_)
            if inputs is not None:
                report.errors.extend(f"{where}.with{problem}" for problem in spec.validate(inputs))
    rollback = step.rollback
    if rollback is None:
        return
    if rollback.compensate:
        spec = catalog.get(step.action) if step.action else None
        if spec is None or spec.compensate is None:
            report.errors.append(
                f"{where}.rollback: true needs an action step whose action can be undone"
                + (f" ('{step.action}' cannot)" if step.action else "")
            )
    if rollback.action:
        target = catalog.get(rollback.action)
        if target is None:
            report.errors.append(f"{where}.rollback.action: unknown action '{rollback.action}'")
        else:
            inputs = _static_inputs(rollback.with_)
            if inputs is not None:
                report.errors.extend(f"{where}.rollback.with{problem}" for problem in target.validate(inputs))
    for index, command in enumerate(rollback.run):
        _check_command(command, f"{where}.rollback[{index}]", report, check_tools=check_tools, needs_approval=True)


def validate_data(
    data: Any,
    *,
    name: str,
    source: Path | None = None,
    loader: WorkflowLoader | None = None,
    check_tools: bool = True,
    base_dir: Path | None = None,
) -> ValidationReport:
    """Validate a raw workflow document (structure + semantics)."""
    report = ValidationReport(workflow=name, source=str(source) if source else None)
    data = normalize_document(data)
    if isinstance(data, dict):
        report.errors.extend(f"steps: duplicate step id '{d}'" for d in _duplicate_ids(data))
        if data.get("name"):
            report.workflow = str(data["name"])
    report.errors.extend(schema_errors(data))
    if report.errors:
        return report
    spec = parse_workflow(data, source=source, key=source.stem if source else name)
    return validate_spec(spec, loader=loader, check_tools=check_tools, base_dir=base_dir, report=report)


def validate_file(
    path: Path, *, loader: WorkflowLoader | None = None, check_tools: bool = True, base_dir: Path | None = None
) -> ValidationReport:
    from highhx.config.loader import load_yaml

    try:
        data = load_yaml(path)
    except ConfigError as exc:
        return ValidationReport(workflow=path.stem, source=str(path), errors=[exc.message])
    if data is None:
        return ValidationReport(workflow=path.stem, source=str(path), errors=["file is empty"])
    return validate_data(data, name=path.stem, source=path, loader=loader, check_tools=check_tools, base_dir=base_dir)

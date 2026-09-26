"""highhx task <name>"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError
from highhx.utils.validation import did_you_mean
from highhx.workflows.dependency_graph import DependencyGraph
from highhx.workflows.parser import parse_workflow


@click.command("task", short_help="Run a task (with its dependencies) from config.")
@click.argument("name", required=False)
@pass_app
def task(app: App, name: str | None) -> int:
    """Run task NAME from `tasks:` in .highhx/config.yaml, running the tasks it
    depends on first (in parallel where possible). Without NAME, list tasks."""
    tasks = app.load_config().tasks
    out = app.output
    if name is None:
        rows = [(t.name, " && ".join(t.run), ", ".join(t.depends_on), t.description) for t in tasks.values()]
        out.emit(
            {
                "tasks": {
                    t.name: {"run": t.run, "depends_on": t.depends_on, "description": t.description}
                    for t in tasks.values()
                }
            },
            lambda: (
                out.table(["task", "run", "depends on", "description"], rows)
                if rows
                else out.info("No tasks configured (add `tasks:` to .highhx/config.yaml).")
            ),
        )
        return 0
    if name not in tasks:
        raise NotFoundError(
            f"Unknown task '{name}'{did_you_mean(name, list(tasks))}.", hint="Run `highhx task` to list tasks."
        )
    graph = DependencyGraph.from_mapping({t.name: t.depends_on for t in tasks.values()})
    selected = [*graph.ancestors(name), name]
    steps = []
    for task_name in graph.topological_order():
        if task_name not in selected:
            continue
        spec = tasks[task_name]
        step: dict[str, object] = {
            "id": task_name,
            "run": spec.run,
            "depends_on": [d for d in spec.depends_on if d in selected],
        }
        if spec.cwd:
            step["cwd"] = spec.cwd
        if spec.env:
            step["env"] = spec.env
        if spec.timeout:
            step["timeout"] = spec.timeout
        steps.append(step)
    workflow = parse_workflow(
        {"name": f"task:{name}", "settings": {"fail_fast": True, "max_parallel": 4}, "steps": steps}, key=f"task-{name}"
    )
    result = app.workflows.run(workflow)
    out.emit(result.to_dict())
    return 0 if result.ok else 1

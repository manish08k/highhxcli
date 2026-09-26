"""highhx workflow graph"""

from __future__ import annotations

import click

from highhx.commands import App, pass_app
from highhx.workflows.dependency_graph import DependencyGraph


@click.command("graph", short_help="Show a workflow's dependency graph and execution stages.")
@click.argument("name")
@click.option("--format", "fmt", type=click.Choice(["text", "dot", "mermaid"]), default="text", show_default=True)
@pass_app
def graph(app: App, name: str, fmt: str) -> int:
    """Print execution stages (steps in the same stage run in parallel), or
    Graphviz DOT / Mermaid source for diagrams."""
    spec = app.workflow_loader.load(name)
    g = DependencyGraph()
    for step in spec.steps:
        g.add_node(step.id)
        for dep in step.depends_on:
            g.add_edge(step.id, dep)
    labels = {}
    for step in spec.steps:
        flags = []
        if step.condition:
            flags.append(f"if {step.condition}")
        if step.approval:
            flags.append("approval")
        if step.uses:
            flags.append(f"uses {step.uses}")
        labels[step.id] = f"[{'; '.join(flags)}]" if flags else ""
    text = {"text": lambda: g.render_text(labels), "dot": lambda: g.to_dot(spec.name), "mermaid": g.to_mermaid}[fmt]()
    app.output.emit(
        {"workflow": spec.name, "stages": g.levels(), "edges": {s.id: s.depends_on for s in spec.steps}, "text": text},
        lambda: click.echo(text),
    )
    return 0

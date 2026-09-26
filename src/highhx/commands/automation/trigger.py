"""highhx trigger"""

from __future__ import annotations

import click

from highhx.automation.triggers import known_events, workflows_for_event
from highhx.commands import App, pass_app
from highhx.core.errors import NotFoundError


@click.command("trigger", short_help="Fire an event and run the workflows subscribed to it.")
@click.argument("event", required=False)
@pass_app
def trigger(app: App, event: str | None) -> int:
    """Run every workflow listening for EVENT (via `on:` in the workflow or
    `triggers:` in config). Without EVENT, list known events."""
    loader = app.workflow_loader
    configured = app.config.triggers
    out = app.output
    if event is None:
        events = known_events(configured, loader)
        out.emit(
            {"events": events},
            lambda: (
                out.table(["event", "workflows"], [(e, ", ".join(w)) for e, w in events.items()])
                if events
                else out.info("No triggers defined.")
            ),
        )
        return 0
    names = workflows_for_event(event, configured, loader)
    if not names:
        raise NotFoundError(
            f"No workflow is triggered by '{event}'.",
            hint="Add `on: [" + event + "]` to a workflow or `triggers:` in config.",
        )
    results = []
    for name in names:
        result = app.workflows.run(name, env={"HIGHHX_EVENT": event})
        results.append(result.to_dict())
        if not result.ok:
            break
    ok = all(r["status"] in ("success", "skipped") for r in results)
    out.emit({"event": event, "ok": ok, "workflows": results})
    return 0 if ok else 1

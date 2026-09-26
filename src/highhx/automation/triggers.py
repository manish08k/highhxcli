"""Event triggers: map event names to workflows."""

from __future__ import annotations

from highhx.workflows.loader import WorkflowLoader


def workflows_for_event(event: str, configured: dict[str, list[str]], loader: WorkflowLoader) -> list[str]:
    """Workflows to run for ``event`` from config ``triggers:`` and workflows' ``on:``."""
    names: list[str] = list(configured.get(event, []))
    for ref in loader.list():
        if event in ref.triggers and ref.key not in names:
            names.append(ref.key)
    return names


def known_events(configured: dict[str, list[str]], loader: WorkflowLoader) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {k: list(v) for k, v in configured.items()}
    for ref in loader.list():
        for event in ref.triggers:
            events.setdefault(event, [])
            if ref.key not in events[event]:
                events[event].append(ref.key)
    return dict(sorted(events.items()))

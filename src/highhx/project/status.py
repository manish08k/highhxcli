"""Aggregated project status (``highhx status``)."""

from __future__ import annotations

from typing import Any

from highhx.core.errors import HighhXError


def collect_status(app: Any) -> dict[str, Any]:
    """Build the status document from an application container (see highhx.commands.App)."""
    profile = app.profile
    data: dict[str, Any] = {
        "project": {
            "name": app.config.project_name or profile.name,
            "root": str(app.root),
            "initialized": app.initialized,
            "stacks": profile.stacks,
            "version": profile.version,
        }
    }
    git: dict[str, Any] = {"available": False}
    repo = app.git_repo
    if repo.is_repo():
        try:
            status = repo.status()
            last = repo.log(limit=1)
            git = {
                "available": True,
                "branch": status.branch,
                "clean": status.clean,
                "changes": status.change_count,
                "ahead": status.ahead,
                "behind": status.behind,
                "upstream": status.upstream,
                "last_commit": f"{last[0].short} {last[0].subject}" if last else None,
            }
        except HighhXError as exc:
            git = {"available": True, "error": exc.message}
    data["git"] = git
    env: dict[str, Any] = {"profile": None, "variables": 0, "missing": []}
    try:
        manager = app.environment
        resolved = manager.resolve()
        checks = manager.check()
        env = {
            "profile": resolved.profile,
            "variables": len(resolved.values),
            "missing": [c.name for c in checks if c.status == "fail" and c.message == "required but not set"],
        }
    except HighhXError as exc:
        env["error"] = exc.message
    data["environment"] = env
    services = []
    if app.initialized:
        try:
            for row in app.services.status():
                services.append(
                    {"name": row.name, "running": row.running, "port": row.port, "pid": row.pid, "healthy": row.healthy}
                )
        except HighhXError:
            pass
    data["services"] = services
    recent = []
    history = app.history
    if history is not None:
        for record in history.list(limit=5):
            recent.append(
                {
                    "id": record.id,
                    "kind": record.kind,
                    "name": record.name,
                    "status": record.status,
                    "duration": record.duration,
                    "started_at": record.started_at,
                }
            )
    data["recent"] = recent
    return data

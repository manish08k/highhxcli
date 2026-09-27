"""Structured project context — deterministic facts, never secrets.

Free uses it for resolution (known services, workflows, deploy targets and environments
become entities: "stop the backend", "deploy staging") and shows it in /context; Pro's
agent sees the same facts. Environment *names* are included, never their values.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.commands import App

IMPORTANT_FILES = (
    "README.md",
    "README.rst",
    "HIGHHX.md",
    "AGENTS.md",
    "pyproject.toml",
    "setup.py",
    "requirements.txt",
    "package.json",
    "tsconfig.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "pubspec.yaml",
    "Dockerfile",
    "docker-compose.yml",
    "compose.yaml",
    "Makefile",
    ".github/workflows",
    ".highhx/config.yaml",
    ".highhx/policies.yaml",
)


def build_context(app: App, *, recent: int = 10) -> dict[str, Any]:
    profile = app.profile
    context: dict[str, Any] = {
        "name": app.config.project_name or profile.name or app.root.name,
        "root": str(app.root),
        "initialized": app.initialized,
        "type": profile.primary,
        "languages": [d.name for d in profile.languages],
        "frameworks": [d.name for d in profile.frameworks],
        "package_managers": [d.name for d in profile.package_managers],
        "databases": [d.name for d in profile.databases],
        "containers": [d.name for d in profile.containers],
        "stack": ", ".join(profile.stacks),
        "important_files": [f for f in IMPORTANT_FILES if (app.root / f).exists()],
        "commands": dict(sorted(app.commands().items())),
        "services": sorted(app.config.services),
        "deploy_targets": sorted(app.config.deploy_targets),
        "workflows": [],
        "environments": [],
        "active_environment": None,
        "git": None,
        "recent_operations": [],
    }
    try:
        context["workflows"] = sorted(ref.key for ref in app.workflow_loader.list())
    except HighhXError:
        pass
    if app.initialized:
        try:
            context["environments"] = app.environment.profile_names()
            context["active_environment"] = app.environment.active_profile()
        except HighhXError:
            pass
    try:
        repo = app.git_repo
        if repo.is_repo():
            status = repo.status()
            context["git"] = {"branch": status.branch, "clean": status.clean, "changes": status.change_count}
    except HighhXError:
        pass
    if app.history is not None:
        try:
            context["recent_operations"] = [
                {"kind": r.kind, "name": r.name, "status": r.status, "at": r.started_at}
                for r in app.history.list(limit=recent)
            ]
        except Exception:
            pass
    return context

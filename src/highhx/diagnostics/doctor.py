"""``highhx doctor``: environment and project health checks."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from highhx.core.errors import HighhXError
from highhx.core.result import CheckResult, CheckStatus
from highhx.detection.operating_system import detect_os
from highhx.detection.runtime import declared_runtimes, satisfies_minimum
from highhx.detection.tools import ToolInfo, detect_tools

CORE_TOOLS = ("python", "git", "docker", "node", "flutter", "java")
STACK_TOOLS = {
    "python": ("python",),
    "node": ("node",),
    "react": ("node",),
    "nextjs": ("node",),
    "flutter": ("flutter", "dart"),
    "dart": ("dart",),
    "java": ("java",),
    "cpp": ("cmake",),
    "go": ("go",),
    "rust": ("cargo",),
    "docker": ("docker",),
}
MANAGER_TOOLS = {
    "pip": "pip",
    "uv": "uv",
    "poetry": "poetry",
    "pdm": "pdm",
    "pipenv": "pipenv",
    "npm": "npm",
    "pnpm": "pnpm",
    "yarn": "yarn",
    "bun": "bun",
    "maven": "mvn",
    "gradle": "gradle",
    "cargo": "cargo",
    "go": "go",
}


def _tool_check(info: ToolInfo, needed: bool, label: str | None = None) -> CheckResult:
    name = label or info.name
    if info.available:
        version = info.version or "installed"
        return CheckResult(f"{name} {version}", CheckStatus.OK, category="tools", data=info.to_dict())
    if needed:
        return CheckResult(
            f"{name} unavailable",
            CheckStatus.WARN if name == "docker" else CheckStatus.FAIL,
            "required by this project",
            hint=f"Install {name} and make sure it is on PATH.",
            category="tools",
        )
    return CheckResult(f"{name} not installed", CheckStatus.SKIP, "not needed by this project", category="tools")


def run_doctor(app: Any) -> list[CheckResult]:
    checks: list[CheckResult] = []
    os_info = detect_os()
    checks.append(CheckResult(os_info.description, CheckStatus.OK, category="system", data=os_info.to_dict()))
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        CheckResult(
            f"HighhX on Python {os_info.python_version}",
            CheckStatus.OK if py_ok else CheckStatus.FAIL,
            category="system",
        )
    )

    profile = app.profile
    needed: set[str] = {"git"}
    for stack in profile.stacks:
        needed.update(STACK_TOOLS.get(stack, ()))
    if profile.has("docker-compose") or any(t.type == "docker" for t in app.config.deploy_targets.values()):
        needed.add("docker")
    wrapped = {d.name for d in profile.package_managers if d.details.get("wrapper")}
    managers = {
        d.name: MANAGER_TOOLS[d.name]
        for d in profile.package_managers
        if d.name in MANAGER_TOOLS and d.name not in wrapped
    }
    names = list(dict.fromkeys([*CORE_TOOLS, *needed, *managers.values()]))
    tools = detect_tools(names)
    for name in names:
        info = tools[name]
        if name == "python" and not info.available:
            info = ToolInfo("python", True, sys.executable, os_info.python_version)
        checks.append(_tool_check(info, name in needed or name in managers.values()))

    if tools.get("docker") and tools["docker"].available:
        from highhx.integrations.docker import DockerClient

        running = DockerClient(app.engine, app.root).daemon_running()
        checks.append(
            CheckResult(
                "Docker daemon running" if running else "Docker daemon not running",
                CheckStatus.OK if running else (CheckStatus.WARN if "docker" in needed else CheckStatus.SKIP),
                hint="Start Docker Desktop / the docker service." if not running else None,
                category="tools",
            )
        )

    for requirement in declared_runtimes(app.root):
        tool = {"python": "python", "node": "node", "dart": "dart", "flutter": "flutter", "java": "java"}.get(
            requirement.runtime
        )
        runtime_info = tools.get(tool) if tool else None
        if requirement.runtime == "python":
            runtime_info = (
                ToolInfo("python", True, sys.executable, os_info.python_version)
                if not (runtime_info and runtime_info.available)
                else runtime_info
            )
        if runtime_info is None or not runtime_info.available or not runtime_info.version:
            continue
        verdict = satisfies_minimum(runtime_info.version, requirement.constraint)
        if verdict is False:
            checks.append(
                CheckResult(
                    f"{requirement.runtime} {runtime_info.version} does not satisfy {requirement.constraint}",
                    CheckStatus.WARN,
                    f"declared in {requirement.source}",
                    hint=f"Install a matching {requirement.runtime} version.",
                    category="runtime",
                )
            )
        elif verdict is True:
            checks.append(
                CheckResult(
                    f"{requirement.runtime} satisfies {requirement.constraint}",
                    CheckStatus.OK,
                    f"declared in {requirement.source}",
                    category="runtime",
                )
            )

    checks.extend(project_checks(app))
    return checks


def project_checks(app: Any) -> list[CheckResult]:
    checks: list[CheckResult] = []
    repo = app.git_repo
    if repo.installed():
        if repo.is_repo():
            status = repo.status()
            checks.append(
                CheckResult(
                    "Git repository",
                    CheckStatus.OK,
                    f"branch {status.branch}, {'clean' if status.clean else f'{status.change_count} change(s)'}",
                    category="project",
                )
            )
        else:
            checks.append(
                CheckResult(
                    "Not a git repository",
                    CheckStatus.WARN,
                    hint="Run `git init` to enable history, releases and hooks.",
                    category="project",
                )
            )
    if not app.initialized:
        checks.append(
            CheckResult("HighhX not initialized", CheckStatus.WARN, hint="Run `highhx init`.", category="project")
        )
    else:
        try:
            app.load_config()
            checks.append(CheckResult("Configuration valid", CheckStatus.OK, category="project"))
        except HighhXError as exc:
            checks.append(
                CheckResult(
                    "Configuration invalid",
                    CheckStatus.FAIL,
                    "; ".join(exc.details[:3]) or exc.message,
                    hint="Run `highhx config validate`.",
                    category="project",
                )
            )
        from highhx.workflows.validator import validate_file

        invalid = [
            r
            for r in (
                validate_file(ref.path, loader=app.workflow_loader, check_tools=False, base_dir=app.root)
                for ref in app.workflow_loader.list()
            )
            if not r.ok
        ]
        if invalid:
            checks.append(
                CheckResult(
                    f"{len(invalid)} invalid workflow(s)",
                    CheckStatus.FAIL,
                    ", ".join(r.workflow for r in invalid),
                    hint="Run `highhx workflow validate`.",
                    category="project",
                )
            )
        else:
            checks.append(
                CheckResult(
                    "Workflows valid",
                    CheckStatus.OK,
                    f"{len(app.workflow_loader.keys())} workflow(s)",
                    category="project",
                )
            )
    for problem in app.profile.problems:
        checks.append(
            CheckResult(
                problem,
                CheckStatus.FAIL,
                hint="Fix the syntax; HighhX ignores unparsable manifests.",
                category="project",
            )
        )
    checks.extend(dependency_checks(app))
    try:
        for check in app.environment.check():
            if check.status == CheckStatus.FAIL:
                checks.append(
                    CheckResult(
                        f"Missing {check.name}"
                        if check.message == "required but not set"
                        else f"{check.name}: {check.message}",
                        CheckStatus.FAIL,
                        f"profile {app.environment.active_profile()}",
                        hint=check.hint,
                        category="environment",
                    )
                )
            elif check.status == CheckStatus.WARN:
                checks.append(CheckResult(check.name, CheckStatus.WARN, check.message, category="environment"))
    except HighhXError as exc:
        checks.append(
            CheckResult("Environment configuration invalid", CheckStatus.FAIL, exc.message, category="environment")
        )
    if app.initialized:
        from highhx.services.ports import is_port_free, port_owner

        states = {s.name: s for s in app.services.status()}
        for name, service in app.config.services.items():
            if service.port is None:
                continue
            state = states.get(name)
            if state and state.running:
                checks.append(
                    CheckResult(f"Service {name} running on :{service.port}", CheckStatus.OK, category="services")
                )
            elif not is_port_free(service.port):
                owner = port_owner(service.port)
                who = f" by {owner.process or 'pid'} {owner.pid}" if owner.pid else ""
                checks.append(
                    CheckResult(
                        f"Port {service.port} for service {name} is in use{who}",
                        CheckStatus.WARN,
                        hint="Stop the other process before `highhx start`.",
                        category="services",
                    )
                )
    usage = shutil.disk_usage(app.root)
    free_gb = usage.free / 1024**3
    checks.append(
        CheckResult(
            f"Disk space {free_gb:.1f} GB free", CheckStatus.OK if free_gb > 2 else CheckStatus.WARN, category="system"
        )
    )
    if not os.access(app.root, os.W_OK):
        checks.append(CheckResult("Project directory is not writable", CheckStatus.FAIL, category="permissions"))
    return checks


def dependency_checks(app: Any) -> list[CheckResult]:
    checks = []
    root = app.root
    for detection in app.profile.package_managers:
        eco = detection.details.get("ecosystem")
        lockfile = detection.details.get("lockfile")
        if eco == "node":
            installed = (root / "node_modules").is_dir()
            checks.append(
                CheckResult(
                    "Node dependencies installed" if installed else "Node dependencies not installed",
                    CheckStatus.OK if installed else CheckStatus.WARN,
                    hint="Run `highhx deps install`." if not installed else None,
                    category="dependencies",
                )
            )
        if eco == "dart":
            installed = (root / ".dart_tool").is_dir()
            checks.append(
                CheckResult(
                    "Dart packages fetched" if installed else "Dart packages not fetched",
                    CheckStatus.OK if installed else CheckStatus.WARN,
                    hint="Run `highhx deps install`." if not installed else None,
                    category="dependencies",
                )
            )
        if lockfile and detection.name not in ("pip",):
            present = (root / lockfile).is_file()
            if not present:
                checks.append(
                    CheckResult(
                        f"{lockfile} missing",
                        CheckStatus.WARN,
                        f"{detection.name} lockfile not found",
                        hint="Run `highhx deps install` to create it, then commit it.",
                        category="dependencies",
                    )
                )
    return checks


def suggested_actions(checks: list[CheckResult]) -> list[str]:
    actions = []
    for check in checks:
        if check.status in (CheckStatus.FAIL, CheckStatus.WARN) and check.hint and check.hint not in actions:
            actions.append(check.hint)
    return actions

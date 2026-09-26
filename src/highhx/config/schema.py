"""Typed configuration models built from validated YAML."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from highhx.config.defaults import (
    DEFAULT_CHANGELOG,
    DEFAULT_DATABASE_URL_ENV,
    DEFAULT_ENV_PROFILE,
    DEFAULT_TAG_PREFIX,
)
from highhx.utils.time import parse_duration


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass
class HealthCheckConfig:
    url: str | None = None
    command: str | None = None
    expected_status: int = 200
    timeout: float = 10.0
    retries: int = 5
    interval: float = 3.0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HealthCheckConfig | None:
        if not data:
            return None
        return cls(
            url=data.get("url"),
            command=data.get("command"),
            expected_status=int(data.get("expected_status", 200)),
            timeout=parse_duration(data.get("timeout", 10)) or 10.0,
            retries=int(data.get("retries", 5)),
            interval=parse_duration(data.get("interval", 3)) or 0.0,
        )


@dataclass
class TaskConfig:
    name: str
    run: list[str]
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None


@dataclass
class ServiceConfig:
    name: str
    command: str
    description: str = ""
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    port: int | None = None
    health: HealthCheckConfig | None = None
    depends_on: list[str] = field(default_factory=list)
    ready_timeout: float = 30.0


@dataclass
class DeployTargetConfig:
    name: str
    type: str
    description: str = ""
    production: bool = False
    require_branch: str | None = None
    require_clean: bool = False
    preflight: list[str] = field(default_factory=list)
    command: str | None = None
    rollback_command: str | None = None
    status_command: str | None = None
    logs_command: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    identity_file: str | None = None
    remote_dir: str | None = None
    compose_file: str | None = None
    service: str | None = None
    image: str | None = None
    build: bool = True
    manifests: str | None = None
    namespace: str | None = None
    context: str | None = None
    deployment: str | None = None
    directory: str | None = None
    vars: dict[str, str] = field(default_factory=dict)
    health_check: HealthCheckConfig | None = None
    auto_rollback: bool = False
    timeout: float | None = None
    env: dict[str, str] = field(default_factory=dict)
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> DeployTargetConfig:
        known = set(cls.__dataclass_fields__) - {"name", "health_check", "timeout", "preflight"}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(
            name=name,
            health_check=HealthCheckConfig.from_dict(data.get("health_check")),
            timeout=parse_duration(data.get("timeout")),
            preflight=_as_list(data.get("preflight")),
            **kwargs,
        )


@dataclass
class DatabaseConfig:
    url_env: str = DEFAULT_DATABASE_URL_ENV
    url: str | None = None
    migrations_command: str | None = None
    migrations_dir: str | None = None
    seed_command: str | None = None
    seed_dir: str | None = None
    backups_dir: str = ".highhx/backups"


@dataclass
class ReleaseConfig:
    tag_prefix: str = DEFAULT_TAG_PREFIX
    changelog: str = DEFAULT_CHANGELOG
    version_files: list[str] = field(default_factory=list)
    publish_command: str | None = None
    commit_message: str = "chore(release): {tag}"
    push: bool = False


@dataclass
class SecurityConfig:
    ignore: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    max_file_size: int = 1_000_000
    scan_untracked: bool = False


@dataclass
class PluginsConfig:
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    allow_code: bool = False
    index: list[str] = field(default_factory=list)


@dataclass
class WatchConfig:
    name: str
    paths: list[str] = field(default_factory=lambda: ["."])
    patterns: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    run: str | None = None
    workflow: str | None = None
    debounce: float = 0.5


@dataclass
class ScheduleConfig:
    name: str
    cron: str
    run: str | None = None
    workflow: str | None = None


@dataclass
class HighhXConfig:
    """The effective project configuration."""

    version: int = 1
    project_name: str | None = None
    project_type: str | None = None
    description: str = ""
    commands: dict[str, str] = field(default_factory=dict)
    scripts: dict[str, str] = field(default_factory=dict)
    tasks: dict[str, TaskConfig] = field(default_factory=dict)
    services: dict[str, ServiceConfig] = field(default_factory=dict)
    default_env_profile: str = DEFAULT_ENV_PROFILE
    deploy_default: str | None = None
    deploy_targets: dict[str, DeployTargetConfig] = field(default_factory=dict)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    approvals: dict[str, Any] = field(default_factory=dict)
    plugins: PluginsConfig = field(default_factory=PluginsConfig)
    watch: list[WatchConfig] = field(default_factory=list)
    schedules: list[ScheduleConfig] = field(default_factory=list)
    triggers: dict[str, list[str]] = field(default_factory=dict)
    hooks: dict[str, str] = field(default_factory=dict)
    workspace_members: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> HighhXConfig:
        """Build from an already validated mapping."""
        data = data or {}
        project = data.get("project") or {}
        deploy = data.get("deploy") or {}
        db = data.get("database") or {}
        release = data.get("release") or {}
        security = data.get("security") or {}
        plugins = data.get("plugins") or {}
        return cls(
            version=int(data.get("version", 1)),
            project_name=project.get("name"),
            project_type=project.get("type"),
            description=project.get("description") or "",
            commands=dict(data.get("commands") or {}),
            scripts=dict(data.get("scripts") or {}),
            tasks={
                name: TaskConfig(
                    name=name,
                    run=_as_list(task.get("run")),
                    description=task.get("description") or "",
                    depends_on=_as_list(task.get("depends_on")),
                    cwd=task.get("cwd"),
                    env=dict(task.get("env") or {}),
                    timeout=parse_duration(task.get("timeout")),
                )
                for name, task in (data.get("tasks") or {}).items()
            },
            services={
                name: ServiceConfig(
                    name=name,
                    command=svc["command"],
                    description=svc.get("description") or "",
                    cwd=svc.get("cwd"),
                    env=dict(svc.get("env") or {}),
                    port=svc.get("port"),
                    health=HealthCheckConfig.from_dict(svc.get("health")),
                    depends_on=_as_list(svc.get("depends_on")),
                    ready_timeout=parse_duration(svc.get("ready_timeout", 30)) or 30.0,
                )
                for name, svc in (data.get("services") or {}).items()
            },
            default_env_profile=(data.get("environment") or {}).get("default_profile") or DEFAULT_ENV_PROFILE,
            deploy_default=deploy.get("default"),
            deploy_targets={
                name: DeployTargetConfig.from_dict(name, target)
                for name, target in (deploy.get("targets") or {}).items()
            },
            database=DatabaseConfig(
                url_env=db.get("url_env") or DEFAULT_DATABASE_URL_ENV,
                url=db.get("url"),
                migrations_command=(db.get("migrations") or {}).get("command"),
                migrations_dir=(db.get("migrations") or {}).get("directory"),
                seed_command=(db.get("seed") or {}).get("command"),
                seed_dir=(db.get("seed") or {}).get("directory"),
                backups_dir=db.get("backups_dir") or ".highhx/backups",
            ),
            release=ReleaseConfig(
                tag_prefix=release.get("tag_prefix", DEFAULT_TAG_PREFIX),
                changelog=release.get("changelog") or DEFAULT_CHANGELOG,
                version_files=_as_list(release.get("version_files")),
                publish_command=release.get("publish_command"),
                commit_message=release.get("commit_message") or "chore(release): {tag}",
                push=bool(release.get("push", False)),
            ),
            security=SecurityConfig(
                ignore=_as_list(security.get("ignore")),
                allowlist=_as_list(security.get("allowlist")),
                max_file_size=int(security.get("max_file_size", 1_000_000)),
                scan_untracked=bool(security.get("scan_untracked", False)),
            ),
            approvals=dict(data.get("approvals") or {}),
            plugins=PluginsConfig(
                enabled=_as_list(plugins.get("enabled")),
                disabled=_as_list(plugins.get("disabled")),
                allow_code=bool(plugins.get("allow_code", False)),
                index=_as_list(plugins.get("index")),
            ),
            watch=[
                WatchConfig(
                    name=w["name"],
                    paths=_as_list(w.get("paths")) or ["."],
                    patterns=_as_list(w.get("patterns")),
                    ignore=_as_list(w.get("ignore")),
                    run=w.get("run"),
                    workflow=w.get("workflow"),
                    debounce=parse_duration(w.get("debounce", 0.5)) or 0.5,
                )
                for w in data.get("watch") or []
            ],
            schedules=[
                ScheduleConfig(name=s["name"], cron=s["cron"], run=s.get("run"), workflow=s.get("workflow"))
                for s in data.get("schedules") or []
            ],
            triggers={k: _as_list(v) for k, v in (data.get("triggers") or {}).items()},
            hooks=dict(data.get("hooks") or {}),
            workspace_members=_as_list((data.get("workspace") or {}).get("members")),
            raw=data,
        )

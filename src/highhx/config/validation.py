"""The configuration schema expressed with :mod:`highhx.utils.validation`."""

from __future__ import annotations

from typing import Any

from highhx.approvals.risk import RISK_NAMES
from highhx.config.defaults import COMMAND_NAMES, DEPLOY_TYPES
from highhx.utils.validation import (
    Bool,
    Duration,
    Int,
    List,
    Map,
    Obj,
    OneOf,
    Prop,
    Str,
    is_env_name,
    is_identifier,
    is_url,
)


def _identifier(value: str) -> str | None:
    return None if is_identifier(value) else "must contain only letters, digits, '_' and '-'"


def _deploy_type(value: str) -> str | None:
    if value in DEPLOY_TYPES or value.startswith("plugin:"):
        return None
    return f"unknown deployment type '{value}' (expected one of {', '.join(DEPLOY_TYPES)} or plugin:<name>)"


def _url(value: str) -> str | None:
    return None if is_url(value) else "must be an http(s) URL"


ENV_MAP = Map(Str(), key_check=is_env_name, key_hint="invalid environment variable name")
COMMAND_OR_LIST = OneOf([Str(min_length=1), List(Str(min_length=1), min_items=1)])

HEALTH_CHECK = Obj(
    {
        "url": Prop(Str(check=_url)),
        "command": Prop(Str(min_length=1)),
        "expected_status": Prop(Int(minimum=100, maximum=599)),
        "timeout": Prop(Duration()),
        "retries": Prop(Int(minimum=1, maximum=100)),
        "interval": Prop(Duration()),
    },
    check=lambda v: [] if ("url" in v or "command" in v) else ["health check needs 'url' or 'command'"],
)

TASK = Obj(
    {
        "description": Prop(Str()),
        "run": Prop(COMMAND_OR_LIST, required=True),
        "depends_on": Prop(List(Str(check=_identifier))),
        "cwd": Prop(Str()),
        "env": Prop(ENV_MAP),
        "timeout": Prop(Duration()),
    }
)

SERVICE = Obj(
    {
        "command": Prop(Str(min_length=1), required=True),
        "description": Prop(Str()),
        "cwd": Prop(Str()),
        "env": Prop(ENV_MAP),
        "port": Prop(Int(minimum=1, maximum=65535)),
        "health": Prop(HEALTH_CHECK),
        "depends_on": Prop(List(Str(check=_identifier))),
        "ready_timeout": Prop(Duration()),
    }
)


def _target_requirements(target: dict[str, Any]) -> list[str]:
    kind = target.get("type")
    required = {
        "local": ["command"],
        "ssh": ["host", "command"],
        "kubernetes": [],
        "docker": [],
        "terraform": [],
    }.get(str(kind), [])
    errors = [f"type '{kind}' requires '{field}'" for field in required if not target.get(field)]
    if kind == "kubernetes" and not (target.get("manifests") or target.get("command")):
        errors.append("type 'kubernetes' requires 'manifests' or 'command'")
    return errors


DEPLOY_TARGET = Obj(
    {
        "type": Prop(Str(check=_deploy_type), required=True),
        "description": Prop(Str()),
        "production": Prop(Bool()),
        "require_branch": Prop(Str()),
        "require_clean": Prop(Bool()),
        "preflight": Prop(List(Str(min_length=1))),
        "command": Prop(Str(min_length=1)),
        "rollback_command": Prop(Str(min_length=1)),
        "status_command": Prop(Str(min_length=1)),
        "logs_command": Prop(Str(min_length=1)),
        "host": Prop(Str(min_length=1)),
        "port": Prop(Int(minimum=1, maximum=65535)),
        "user": Prop(Str()),
        "identity_file": Prop(Str()),
        "remote_dir": Prop(Str()),
        "compose_file": Prop(Str()),
        "service": Prop(Str()),
        "image": Prop(Str()),
        "build": Prop(Bool()),
        "manifests": Prop(Str()),
        "namespace": Prop(Str()),
        "context": Prop(Str()),
        "deployment": Prop(Str()),
        "directory": Prop(Str()),
        "vars": Prop(Map(Str())),
        "health_check": Prop(HEALTH_CHECK),
        "auto_rollback": Prop(Bool()),
        "timeout": Prop(Duration()),
        "env": Prop(ENV_MAP),
        "settings": Prop(Map()),
    },
    check=_target_requirements,
)


CONFIG_SCHEMA = Obj(
    {
        "version": Prop(Int(minimum=1, maximum=1), description="Config format version"),
        "project": Prop(
            Obj(
                {
                    "name": Prop(Str(min_length=1)),
                    "type": Prop(Str()),
                    "description": Prop(Str()),
                }
            )
        ),
        "commands": Prop(
            Obj({name: Prop(Str(min_length=1)) for name in COMMAND_NAMES}),
            description="Override the commands HighhX detected",
        ),
        "scripts": Prop(Map(Str(min_length=1), key_check=is_identifier, key_hint="invalid script name")),
        "tasks": Prop(Map(TASK, key_check=is_identifier, key_hint="invalid task name")),
        "services": Prop(Map(SERVICE, key_check=is_identifier, key_hint="invalid service name")),
        "environment": Prop(Obj({"default_profile": Prop(Str(check=_identifier))})),
        "deploy": Prop(
            Obj(
                {
                    "default": Prop(Str(check=_identifier)),
                    "targets": Prop(Map(DEPLOY_TARGET, key_check=is_identifier, key_hint="invalid target name")),
                }
            )
        ),
        "database": Prop(
            Obj(
                {
                    "url_env": Prop(Str(check=lambda v: None if is_env_name(v) else "invalid variable name")),
                    "url": Prop(Str()),
                    "migrations": Prop(Obj({"command": Prop(Str(min_length=1)), "directory": Prop(Str())})),
                    "seed": Prop(Obj({"command": Prop(Str(min_length=1)), "directory": Prop(Str())})),
                    "backups_dir": Prop(Str()),
                }
            )
        ),
        "release": Prop(
            Obj(
                {
                    "tag_prefix": Prop(Str()),
                    "changelog": Prop(Str()),
                    "version_files": Prop(List(Str())),
                    "publish_command": Prop(Str(min_length=1)),
                    "commit_message": Prop(Str()),
                    "push": Prop(Bool()),
                }
            )
        ),
        "security": Prop(
            Obj(
                {
                    "ignore": Prop(List(Str())),
                    "allowlist": Prop(List(Str())),
                    "max_file_size": Prop(Int(minimum=1024)),
                    "scan_untracked": Prop(Bool()),
                }
            )
        ),
        "approvals": Prop(
            Obj(
                {
                    "auto_approve": Prop(Str(choices=RISK_NAMES)),
                    "yes_max_risk": Prop(Str(choices=RISK_NAMES)),
                    "typed_confirmation": Prop(Str(choices=RISK_NAMES)),
                    "non_bypassable": Prop(List(Str())),
                    "rules": Prop(
                        List(
                            Obj(
                                {
                                    "id": Prop(Str(check=_identifier)),
                                    "pattern": Prop(Str(min_length=1), required=True),
                                    "risk": Prop(Str(choices=RISK_NAMES)),
                                    "reason": Prop(Str()),
                                    "bypassable": Prop(Bool()),
                                }
                            )
                        )
                    ),
                }
            )
        ),
        "plugins": Prop(
            Obj(
                {
                    "enabled": Prop(List(Str())),
                    "disabled": Prop(List(Str())),
                    "allow_code": Prop(Bool()),
                    "index": Prop(List(Str())),
                }
            )
        ),
        "watch": Prop(
            List(
                Obj(
                    {
                        "name": Prop(Str(check=_identifier), required=True),
                        "paths": Prop(List(Str())),
                        "patterns": Prop(List(Str())),
                        "ignore": Prop(List(Str())),
                        "run": Prop(Str(min_length=1)),
                        "workflow": Prop(Str(min_length=1)),
                        "debounce": Prop(Duration()),
                    },
                    check=lambda w: (
                        [] if ("run" in w) ^ ("workflow" in w) else ["exactly one of 'run' or 'workflow' is required"]
                    ),
                )
            )
        ),
        "schedules": Prop(
            List(
                Obj(
                    {
                        "name": Prop(Str(check=_identifier), required=True),
                        "cron": Prop(Str(min_length=1), required=True),
                        "run": Prop(Str(min_length=1)),
                        "workflow": Prop(Str(min_length=1)),
                    },
                    check=lambda w: (
                        [] if ("run" in w) ^ ("workflow" in w) else ["exactly one of 'run' or 'workflow' is required"]
                    ),
                )
            )
        ),
        "triggers": Prop(Map(List(Str(min_length=1)))),
        "hooks": Prop(Map(Str(min_length=1))),
        "workspace": Prop(Obj({"members": Prop(List(Str()))})),
        "agent": Prop(
            Obj(
                {
                    "provider": Prop(Str(choices=("highhx", "anthropic", "openai", "gemini"))),
                    "model": Prop(Str(min_length=1)),
                    "approval": Prop(Str(choices=("ask", "auto-edit", "read-only"))),
                    "max_steps": Prop(Int(minimum=1, maximum=500)),
                    "max_tokens": Prop(Int(minimum=1024, maximum=128_000)),
                    "effort": Prop(Str(choices=("low", "medium", "high", "xhigh", "max"))),
                    "instructions": Prop(Str(), description="Extra instructions for the HighhX agent in this project"),
                    "sync_sessions": Prop(Bool()),
                },
                description="HighhX Pro agent settings for this project",
            )
        ),
    }
)


def validate_config(data: Any) -> list[str]:
    """Validate a parsed config document; returns all problems found."""
    if data is None:
        return []
    errors = CONFIG_SCHEMA.validate(data, "")
    if errors:
        return errors
    deploy = data.get("deploy") or {}
    default = deploy.get("default")
    if default and default not in (deploy.get("targets") or {}):
        errors.append(f"deploy.default: target '{default}' is not defined in deploy.targets")
    tasks = data.get("tasks") or {}
    for name, task in tasks.items():
        for dep in task.get("depends_on") or []:
            if dep not in tasks:
                errors.append(f"tasks.{name}.depends_on: unknown task '{dep}'")
    services = data.get("services") or {}
    for name, service in services.items():
        for dep in service.get("depends_on") or []:
            if dep not in services:
                errors.append(f"services.{name}.depends_on: unknown service '{dep}'")
    if (data.get("database") or {}).get("url"):
        url = data["database"]["url"]
        if "@" in url and ":" in url.split("@", 1)[0].split("//", 1)[-1]:
            errors.append("database.url: contains a password; use database.url_env and an environment variable instead")
    from highhx.automation.cron import CronExpression

    for index, schedule in enumerate(data.get("schedules") or []):
        try:
            CronExpression.parse(schedule["cron"])
        except ValueError as exc:
            errors.append(f"schedules[{index}].cron: {exc}")
    return errors

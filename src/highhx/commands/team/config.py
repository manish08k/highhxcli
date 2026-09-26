"""highhx config"""

from __future__ import annotations

import click
import yaml

from highhx.commands import App, pass_app
from highhx.commands.groups import DefaultGroup
from highhx.config.loader import load_config, read_config_data
from highhx.config.validation import validate_config
from highhx.core.errors import ConfigError, NotFoundError
from highhx.environment.variables import ENVIRONMENT_SCHEMA
from highhx.policy.validator import validate_policies


@click.group("config", cls=DefaultGroup, default_command="show", short_help="Show and validate configuration.")
def config() -> None:
    """Project configuration lives in .highhx/config.yaml (see docs/configuration.md)."""


@config.command("show", short_help="Print the effective configuration (with --config-profile overlay).")
@pass_app
def show(app: App) -> int:
    """Print the effective configuration (config.yaml merged with --config-profile) and the
    files it came from."""
    app.require_project()
    data, sources = read_config_data(app.root, app.options.profile)

    def render() -> None:
        app.output.note("sources: " + ", ".join(str(s.relative_to(app.root)) for s in sources))
        app.output.plain(yaml.safe_dump(data, sort_keys=False))

    app.output.emit(
        {"sources": [str(s) for s in sources], "config": data},
        render,
    )
    return 0


@config.command("validate", short_help="Validate config, environment, policies and workflows.")
@pass_app
def validate(app: App) -> int:
    """Validate every configuration file and report all problems at once."""
    app.require_project()
    from highhx.config.loader import load_yaml
    from highhx.workflows.validator import validate_file

    results: list[dict[str, object]] = []

    def record(name: str, errors: list[str], warnings: list[str] | None = None) -> None:
        results.append({"file": name, "valid": not errors, "errors": errors, "warnings": warnings or []})

    try:
        data, _ = read_config_data(app.root, app.options.profile)
        record("config.yaml", validate_config(data))
    except ConfigError as exc:
        record("config.yaml", [exc.message, *exc.details])
    for name, path, validator in (
        (
            "environment.yaml",
            app.paths.environment_file,
            lambda d: ENVIRONMENT_SCHEMA.validate(d, "") if d is not None else [],
        ),
        ("policies.yaml", app.paths.policies_file, validate_policies),
    ):
        if path.exists():
            try:
                record(name, validator(load_yaml(path)))
            except ConfigError as exc:
                record(name, [exc.message])
    for ref in app.workflow_loader.list():
        report = validate_file(ref.path, loader=app.workflow_loader, check_tools=False, base_dir=app.root)
        record(f"workflows/{ref.path.name}", report.errors, report.warnings)
    ok = all(r["valid"] for r in results)
    out = app.output

    def render() -> None:
        for r in results:
            if r["valid"]:
                out.success(f"{r['file']}: valid")
            else:
                out.error(f"{r['file']}: {len(r['errors'])} problem(s)")  # type: ignore[arg-type]
                for error in r["errors"]:  # type: ignore[attr-defined]
                    out.plain(f"    {error}")
            for warning in r["warnings"]:  # type: ignore[attr-defined]
                out.warn(f"    {warning}")

    out.emit({"valid": ok, "files": results}, render)
    return 0 if ok else 3


@config.command("get", short_help="Print one value by dotted path (e.g. commands.test).")
@click.argument("key")
@pass_app
def get(app: App, key: str) -> int:
    """Print the configuration value at a dotted KEY such as commands.test or deploy.default."""
    app.require_project()
    data = load_config(app.root, profile=app.options.profile).data
    current: object = data
    for part in key.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise NotFoundError(f"'{key}' is not set in the configuration.")
    app.output.emit(
        {"key": key, "value": current},
        lambda: click.echo(
            yaml.safe_dump(current, sort_keys=False).strip() if isinstance(current, dict | list) else current
        ),
    )
    return 0


@config.command("path", short_help="Print the configuration file path.")
@pass_app
def path(app: App) -> int:
    """Print the path of .highhx/config.yaml (exit code 4 if it does not exist)."""
    app.output.emit(
        {"path": str(app.paths.config_file), "exists": app.paths.config_file.exists()},
        lambda: click.echo(str(app.paths.config_file)),
    )
    return 0 if app.paths.config_file.exists() else 4


@config.command("schema", short_help="Print the JSON Schema of config.yaml or workflow files.")
@click.argument("kind", type=click.Choice(["config", "workflow", "plugin"]), default="config")
def schema(kind: str) -> int:
    """Print the JSON Schema for config.yaml, workflow files or plugin manifests, for editor
    validation and autocompletion."""
    import json

    if kind == "config":
        from highhx.config.validation import CONFIG_SCHEMA

        document = CONFIG_SCHEMA.json_schema() | {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "HighhX config",
        }
    elif kind == "workflow":
        from highhx.workflows.schema import workflow_json_schema

        document = workflow_json_schema()
    else:
        from highhx.plugins.manifest import MANIFEST_SCHEMA

        document = MANIFEST_SCHEMA.json_schema() | {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "HighhX plugin manifest",
        }
    click.echo(json.dumps(document, indent=2))
    return 0

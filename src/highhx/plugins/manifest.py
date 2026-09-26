"""Plugin manifest (``highhx-plugin.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.approvals.risk import RISK_NAMES
from highhx.config.loader import load_yaml
from highhx.core.errors import ConfigError, PluginError
from highhx.utils.validation import Int, List, Obj, Prop, Str, is_identifier

MANIFEST_NAMES = ("highhx-plugin.yaml", "highhx-plugin.yml")
API_VERSION = 1
PERMISSIONS = ("commands", "subprocess", "network", "filesystem", "env", "deploy", "detectors")


def _name(value: str) -> str | None:
    return None if is_identifier(value) else "plugin names use letters, digits, '-' and '_'"


def _entry(value: str) -> str | None:
    if ":" not in value or not value.split(":", 1)[0].endswith(".py"):
        return "entry must look like 'plugin.py:ClassName'"
    return None


COMMAND_SCHEMA = Obj(
    {
        "name": Prop(Str(check=_name), required=True),
        "description": Prop(Str()),
        "run": Prop(Str(min_length=1), required=True),
        "risk": Prop(Str(choices=RISK_NAMES)),
    }
)
DETECTOR_SCHEMA = Obj(
    {
        "name": Prop(Str(min_length=1), required=True),
        "kind": Prop(Str(choices=("language", "framework", "package_manager", "database", "container"))),
        "files": Prop(List(Str(), min_items=1), required=True),
    }
)
MANIFEST_SCHEMA = Obj(
    {
        "name": Prop(Str(check=_name), required=True),
        "version": Prop(Str(min_length=1), required=True),
        "description": Prop(Str()),
        "author": Prop(Str()),
        "homepage": Prop(Str()),
        "license": Prop(Str()),
        "api_version": Prop(Int(minimum=1), required=True),
        "entry": Prop(Str(check=_entry)),
        "permissions": Prop(List(Str(choices=PERMISSIONS), unique=True)),
        "contributes": Prop(
            Obj(
                {
                    "commands": Prop(List(COMMAND_SCHEMA)),
                    "workflows": Prop(List(Str())),
                    "templates": Prop(List(Str())),
                    "detectors": Prop(List(DETECTOR_SCHEMA)),
                }
            )
        ),
    }
)


@dataclass
class PluginCommand:
    name: str
    run: str
    description: str = ""
    risk: str = "normal"


@dataclass
class PluginDetector:
    name: str
    files: list[str]
    kind: str = "language"


@dataclass
class PluginManifest:
    name: str
    version: str
    api_version: int
    directory: Path
    description: str = ""
    author: str = ""
    entry: str | None = None
    permissions: list[str] = field(default_factory=list)
    commands: list[PluginCommand] = field(default_factory=list)
    workflow_dirs: list[str] = field(default_factory=list)
    template_dirs: list[str] = field(default_factory=list)
    detectors: list[PluginDetector] = field(default_factory=list)

    @property
    def has_code(self) -> bool:
        return self.entry is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "description": self.description,
            "author": self.author,
            "entry": self.entry,
            "permissions": self.permissions,
            "commands": [c.name for c in self.commands],
            "workflows": self.workflow_dirs,
            "templates": self.template_dirs,
            "detectors": [d.name for d in self.detectors],
            "directory": str(self.directory),
        }


def manifest_path(directory: Path) -> Path | None:
    for name in MANIFEST_NAMES:
        if (directory / name).is_file():
            return directory / name
    return None


def validate_manifest_data(data: Any, directory: Path) -> list[str]:
    errors = MANIFEST_SCHEMA.validate(data, "") if isinstance(data, dict) else ["manifest must be a mapping"]
    if errors:
        return errors
    if data["api_version"] > API_VERSION:
        errors.append(
            f"api_version {data['api_version']} is newer than this HighhX supports ({API_VERSION}); upgrade HighhX"
        )
    entry = data.get("entry")
    if entry:
        file = entry.split(":", 1)[0]
        target = (directory / file).resolve()
        if not target.is_file():
            errors.append(f"entry: {file} does not exist")
        elif directory.resolve() not in target.parents:
            errors.append("entry: must point inside the plugin directory")
        if (
            "commands" not in (data.get("permissions") or [])
            and "deploy" not in (data.get("permissions") or [])
            and "detectors" not in (data.get("permissions") or [])
        ):
            errors.append(
                "entry: code plugins must declare what they contribute in permissions (commands, deploy or detectors)"
            )
    contributes = data.get("contributes") or {}
    for key in ("workflows", "templates"):
        for rel in contributes.get(key) or []:
            path = (directory / rel).resolve()
            if not path.is_dir():
                errors.append(f"contributes.{key}: directory '{rel}' does not exist")
            elif directory.resolve() not in (path, *path.parents):
                errors.append(f"contributes.{key}: '{rel}' is outside the plugin")
    if contributes.get("commands") and "commands" not in (data.get("permissions") or []):
        errors.append("contributes.commands requires the 'commands' permission")
    return errors


def load_manifest(directory: Path) -> PluginManifest:
    path = manifest_path(directory)
    if path is None:
        raise PluginError(
            f"{directory} has no highhx-plugin.yaml manifest.", hint="See docs/plugins.md for the manifest format."
        )
    try:
        data = load_yaml(path)
    except ConfigError as exc:
        raise PluginError(exc.message) from exc
    errors = validate_manifest_data(data, directory)
    if errors:
        raise PluginError(f"Invalid plugin manifest in {directory.name}", details=errors)
    contributes = data.get("contributes") or {}
    return PluginManifest(
        name=data["name"],
        version=str(data["version"]),
        api_version=int(data["api_version"]),
        directory=directory,
        description=data.get("description") or "",
        author=data.get("author") or "",
        entry=data.get("entry"),
        permissions=list(data.get("permissions") or []),
        commands=[
            PluginCommand(c["name"], c["run"], c.get("description") or "", c.get("risk", "normal"))
            for c in contributes.get("commands") or []
        ],
        workflow_dirs=list(contributes.get("workflows") or []),
        template_dirs=list(contributes.get("templates") or []),
        detectors=[
            PluginDetector(d["name"], list(d["files"]), d.get("kind", "language"))
            for d in contributes.get("detectors") or []
        ],
    )

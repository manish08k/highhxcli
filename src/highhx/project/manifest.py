"""Parsers for project manifests (pyproject.toml, package.json, pubspec.yaml, pom.xml …)."""

from __future__ import annotations

import json
import re
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from highhx.utils.filesystem import read_text

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_name(name: str) -> str:
    """PEP 503-style normalisation (also fine for npm/pub names)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def read_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(read_text(path))
    except (tomllib.TOMLDecodeError, OSError):
        return None


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text(path))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def read_yaml_mapping(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(read_text(path))
    except (yaml.YAMLError, OSError):
        return None
    return data if isinstance(data, dict) else None


def requirement_name(line: str) -> str | None:
    """Package name from a requirements.txt / PEP 508 line."""
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http:", "https:", "file:")):
        return None
    match = _REQ_NAME_RE.match(line)
    return normalize_name(match.group(1)) if match else None


def python_dependencies(root: Path) -> set[str]:
    """All declared Python dependency names (runtime, optional, dev, requirements files)."""
    names: set[str] = set()
    pyproject = read_toml(root / "pyproject.toml") or {}
    project = pyproject.get("project") or {}
    for item in project.get("dependencies") or []:
        if (name := requirement_name(str(item))) is not None:
            names.add(name)
    for group in (project.get("optional-dependencies") or {}).values():
        for item in group or []:
            if (name := requirement_name(str(item))) is not None:
                names.add(name)
    for group in (pyproject.get("dependency-groups") or {}).values():
        for item in group or []:
            if isinstance(item, str) and (name := requirement_name(item)) is not None:
                names.add(name)
    poetry = (pyproject.get("tool") or {}).get("poetry") or {}
    for section in ("dependencies", "dev-dependencies"):
        names.update(normalize_name(n) for n in (poetry.get(section) or {}) if n.lower() != "python")
    for group in (poetry.get("group") or {}).values():
        names.update(normalize_name(n) for n in (group.get("dependencies") or {}))
    for req in sorted(root.glob("requirements*.txt")) + sorted(root.glob("requirements/*.txt")):
        try:
            for line in read_text(req).splitlines():
                if (name := requirement_name(line)) is not None:
                    names.add(name)
        except OSError:
            continue
    pipfile = read_toml(root / "Pipfile") or {}
    for section in ("packages", "dev-packages"):
        names.update(normalize_name(n) for n in (pipfile.get(section) or {}))
    return names


def node_dependencies(package: dict[str, Any] | None) -> set[str]:
    if not package:
        return set()
    names: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        names.update((package.get(section) or {}).keys())
    return names


def pubspec_dependencies(pubspec: dict[str, Any] | None) -> set[str]:
    if not pubspec:
        return set()
    names: set[str] = set()
    for section in ("dependencies", "dev_dependencies"):
        names.update((pubspec.get(section) or {}).keys())
    return names


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


MAX_XML_BYTES = 5 * 1024 * 1024
_XML_DTD = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def parse_xml(path: Path) -> ET.ElementTree[ET.Element]:
    """Parse a project XML manifest from a possibly untrusted repository.

    Manifests never need a DTD, so documents that declare one (or entities) are refused
    outright — no entity expansion ("billion laughs") or external entity can occur,
    independent of the XML library's own limits — and so are oversized files.
    """
    data = path.read_bytes()
    if len(data) > MAX_XML_BYTES:
        raise ET.ParseError(f"{path.name} is larger than {MAX_XML_BYTES // (1024 * 1024)} MB")
    if _XML_DTD.search(data):
        raise ET.ParseError(f"{path.name} declares a DTD or entities, which are not allowed")
    parser = ET.XMLParser()  # nosec B314 - DTD/entity declarations and oversized input are refused above
    parser.feed(data)
    return ET.ElementTree(parser.close())


def read_pom(path: Path) -> dict[str, Any] | None:
    """Minimal pom.xml reader: groupId, artifactId, version, dependency artifactIds."""
    if not path.is_file():
        return None
    try:
        tree = parse_xml(path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    info: dict[str, Any] = {"dependencies": [], "modules": []}
    for child in root:
        tag = _strip_ns(child.tag)
        if tag in ("groupId", "artifactId", "version", "name", "description", "packaging"):
            info[tag] = (child.text or "").strip()
        elif tag == "dependencies":
            for dep in child:
                for part in dep:
                    if _strip_ns(part.tag) == "artifactId":
                        info["dependencies"].append((part.text or "").strip())
        elif tag == "modules":
            info["modules"] = [(m.text or "").strip() for m in child]
        elif tag == "parent":
            for part in child:
                if _strip_ns(part.tag) == "artifactId":
                    info["parent"] = (part.text or "").strip()
    return info


@dataclass
class ProjectManifest:
    """Normalised view of the primary manifest."""

    source: str
    name: str | None = None
    version: str | None = None
    description: str | None = None
    scripts: dict[str, str] = field(default_factory=dict)
    dependencies: set[str] = field(default_factory=set)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "scripts": self.scripts,
            "dependencies": sorted(self.dependencies),
        }


def load_manifests(root: Path) -> list[ProjectManifest]:
    """All recognised manifests at ``root`` in priority order."""
    manifests: list[ProjectManifest] = []
    pyproject = read_toml(root / "pyproject.toml")
    if pyproject is not None:
        project = pyproject.get("project") or {}
        poetry = (pyproject.get("tool") or {}).get("poetry") or {}
        scripts = dict(((pyproject.get("tool") or {}).get("highhx") or {}).get("scripts") or {})
        manifests.append(
            ProjectManifest(
                source="pyproject.toml",
                name=project.get("name") or poetry.get("name"),
                version=project.get("version") or poetry.get("version"),
                description=project.get("description") or poetry.get("description"),
                scripts={k: str(v) for k, v in scripts.items()},
                dependencies=python_dependencies(root),
            )
        )
    elif (root / "setup.py").is_file() or list(root.glob("requirements*.txt")):
        manifests.append(
            ProjectManifest(
                source="requirements.txt" if list(root.glob("requirements*.txt")) else "setup.py",
                dependencies=python_dependencies(root),
            )
        )
    package = read_json(root / "package.json")
    if package is not None:
        manifests.append(
            ProjectManifest(
                source="package.json",
                name=package.get("name"),
                version=package.get("version"),
                description=package.get("description"),
                scripts={k: str(v) for k, v in (package.get("scripts") or {}).items()},
                dependencies=node_dependencies(package),
            )
        )
    pubspec = read_yaml_mapping(root / "pubspec.yaml")
    if pubspec is not None:
        manifests.append(
            ProjectManifest(
                source="pubspec.yaml",
                name=pubspec.get("name"),
                version=str(pubspec.get("version")) if pubspec.get("version") is not None else None,
                description=pubspec.get("description"),
                dependencies=pubspec_dependencies(pubspec),
            )
        )
    pom = read_pom(root / "pom.xml")
    if pom is not None:
        manifests.append(
            ProjectManifest(
                source="pom.xml",
                name=pom.get("artifactId"),
                version=pom.get("version"),
                description=pom.get("description"),
                dependencies=set(pom.get("dependencies") or []),
            )
        )
    for gradle in ("build.gradle.kts", "build.gradle"):
        if (root / gradle).is_file():
            text = read_text(root / gradle)
            version = re.search(r"^\s*version\s*=?\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
            deps = set(re.findall(r"['\"][\w.-]+:([\w.-]+):", text))
            settings = (
                root / "settings.gradle.kts" if (root / "settings.gradle.kts").exists() else root / "settings.gradle"
            )
            name = None
            if settings.is_file():
                m = re.search(r"rootProject\.name\s*=\s*['\"]([^'\"]+)['\"]", read_text(settings))
                name = m.group(1) if m else None
            manifests.append(
                ProjectManifest(
                    source=gradle, name=name, version=version.group(1) if version else None, dependencies=deps
                )
            )
            break
    cargo = read_toml(root / "Cargo.toml")
    if cargo is not None:
        pkg = cargo.get("package") or {}
        manifests.append(
            ProjectManifest(
                source="Cargo.toml",
                name=pkg.get("name"),
                version=pkg.get("version") if isinstance(pkg.get("version"), str) else None,
                description=pkg.get("description"),
                dependencies=set((cargo.get("dependencies") or {}).keys()),
            )
        )
    if (root / "go.mod").is_file():
        m = re.search(r"^module\s+(\S+)", read_text(root / "go.mod"), re.MULTILINE)
        manifests.append(ProjectManifest(source="go.mod", name=m.group(1).rsplit("/", 1)[-1] if m else None))
    if (root / "CMakeLists.txt").is_file():
        text = read_text(root / "CMakeLists.txt")
        m = re.search(r"project\s*\(\s*([\w.-]+)(?:[^)]*VERSION\s+([\d.]+))?", text, re.IGNORECASE)
        manifests.append(
            ProjectManifest(source="CMakeLists.txt", name=m.group(1) if m else None, version=m.group(2) if m else None)
        )
    return manifests


def load_manifest(root: Path) -> ProjectManifest | None:
    manifests = load_manifests(root)
    return manifests[0] if manifests else None


def manifest_problems(root: Path) -> list[str]:
    """Manifests that exist but cannot be parsed (detection silently skips them otherwise)."""
    problems: list[str] = []
    checks: list[tuple[str, Any]] = [
        ("pyproject.toml", tomllib.loads),
        ("Cargo.toml", tomllib.loads),
        ("Pipfile", tomllib.loads),
        ("package.json", json.loads),
        ("composer.json", json.loads),
        ("pubspec.yaml", yaml.safe_load),
    ]
    for name, parser in checks:
        path = root / name
        if not path.is_file():
            continue
        try:
            data = parser(read_text(path))
        except (tomllib.TOMLDecodeError, json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
            first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            problems.append(f"{name}: cannot be parsed ({first})")
            continue
        if not isinstance(data, dict):
            problems.append(f"{name}: expected a mapping at the top level")
    pom = root / "pom.xml"
    if pom.is_file():
        try:
            parse_xml(pom)
        except (ET.ParseError, OSError) as exc:
            problems.append(f"pom.xml: cannot be parsed ({exc})")
    return problems

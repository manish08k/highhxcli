"""Runtime requirements declared by a project (e.g. required Python / Node versions)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from highhx.project.manifest import read_json, read_toml, read_yaml_mapping
from highhx.utils.filesystem import read_text


@dataclass(frozen=True)
class RuntimeRequirement:
    runtime: str
    constraint: str
    source: str


def declared_runtimes(root: Path) -> list[RuntimeRequirement]:
    """Collect declared runtime version constraints."""
    found: list[RuntimeRequirement] = []
    pyproject = read_toml(root / "pyproject.toml") or {}
    requires = (pyproject.get("project") or {}).get("requires-python")
    if requires:
        found.append(RuntimeRequirement("python", str(requires), "pyproject.toml"))
    for name in (".python-version",):
        if (root / name).is_file():
            found.append(RuntimeRequirement("python", read_text(root / name).strip().splitlines()[0], name))
    package = read_json(root / "package.json") or {}
    engines = package.get("engines") or {}
    if engines.get("node"):
        found.append(RuntimeRequirement("node", str(engines["node"]), "package.json"))
    for name in (".nvmrc", ".node-version"):
        if (root / name).is_file():
            content = read_text(root / name).strip()
            if content:
                found.append(RuntimeRequirement("node", content.splitlines()[0], name))
    pubspec = read_yaml_mapping(root / "pubspec.yaml") or {}
    env = pubspec.get("environment") or {}
    if env.get("sdk"):
        found.append(RuntimeRequirement("dart", str(env["sdk"]), "pubspec.yaml"))
    if env.get("flutter"):
        found.append(RuntimeRequirement("flutter", str(env["flutter"]), "pubspec.yaml"))
    if (root / ".java-version").is_file():
        found.append(RuntimeRequirement("java", read_text(root / ".java-version").strip(), ".java-version"))
    return found


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.strip().lstrip("v^~>=<!").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def satisfies_minimum(installed: str, constraint: str) -> bool | None:
    """Best-effort check of simple constraints (``>=3.11``, ``^18``, ``20``, ``>=18 <21``).

    Returns None when the constraint is too complex to evaluate reliably.
    """
    have = _version_tuple(installed)
    if not have:
        return None
    ok = True
    evaluated = False
    for clause in constraint.replace(",", " ").split():
        clause = clause.strip()
        if clause.startswith(">="):
            want = _version_tuple(clause[2:])
            ok &= have >= want
        elif clause.startswith(">"):
            want = _version_tuple(clause[1:])
            ok &= have > want
        elif clause.startswith("<="):
            want = _version_tuple(clause[2:])
            ok &= have[: len(want)] <= want
        elif clause.startswith("<"):
            want = _version_tuple(clause[1:])
            ok &= have < want
        elif clause.startswith(("^", "~")):
            want = _version_tuple(clause[1:])
            ok &= bool(want) and have[0] == want[0] and have >= want
        elif clause[:1].isdigit() or clause.startswith("v"):
            want = _version_tuple(clause)
            ok &= have[: len(want)] == want
        else:
            return None
        evaluated = True
    return ok if evaluated else None

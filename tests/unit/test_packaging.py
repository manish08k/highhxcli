"""Guard against ignore rules silently dropping package code from the wheel."""

import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_gitignore_does_not_exclude_package_directories() -> None:
    patterns = [
        line.strip().rstrip("/")
        for line in (ROOT / ".gitignore").read_text().splitlines()
        if line.strip() and not line.startswith(("#", "!")) and not line.strip().startswith("/")
    ]
    package = ROOT / "src" / "highhx"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in package.rglob("*")
        if "__pycache__" not in path.parts and any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)
    ]
    assert offenders == []


def test_templates_and_schemas_are_packaged() -> None:
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    force = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force == {"templates": "highhx/_templates", "schemas": "highhx/_schemas"}

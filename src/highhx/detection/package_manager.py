"""Package manager detection."""

from __future__ import annotations

from pathlib import Path

from highhx.detection import Detection
from highhx.project.manifest import read_json, read_toml
from highhx.utils.platform import executable_name


def detect_package_managers(root: Path) -> list[Detection]:
    """Detect package managers, preferring lockfiles over manifests."""
    found: list[Detection] = []

    # ---- Python
    pyproject = read_toml(root / "pyproject.toml") or {}
    tool = pyproject.get("tool") or {}
    if (root / "uv.lock").is_file() or "uv" in tool:
        found.append(
            Detection(
                "uv",
                "package_manager",
                ["uv.lock" if (root / "uv.lock").is_file() else "[tool.uv]"],
                details={"ecosystem": "python", "lockfile": "uv.lock"},
            )
        )
    elif (root / "poetry.lock").is_file() or "poetry" in tool:
        found.append(
            Detection(
                "poetry",
                "package_manager",
                ["poetry.lock" if (root / "poetry.lock").is_file() else "[tool.poetry]"],
                details={"ecosystem": "python", "lockfile": "poetry.lock"},
            )
        )
    elif (root / "pdm.lock").is_file() or "pdm" in tool:
        found.append(
            Detection(
                "pdm",
                "package_manager",
                ["pdm.lock" if (root / "pdm.lock").is_file() else "[tool.pdm]"],
                details={"ecosystem": "python", "lockfile": "pdm.lock"},
            )
        )
    elif (root / "Pipfile").is_file():
        found.append(
            Detection(
                "pipenv", "package_manager", ["Pipfile"], details={"ecosystem": "python", "lockfile": "Pipfile.lock"}
            )
        )
    elif (root / "pyproject.toml").is_file() or list(root.glob("requirements*.txt")) or (root / "setup.py").is_file():
        evidence = [p.name for p in sorted(root.glob("requirements*.txt"))] or [
            "pyproject.toml" if (root / "pyproject.toml").is_file() else "setup.py"
        ]
        found.append(Detection("pip", "package_manager", evidence, details={"ecosystem": "python", "lockfile": None}))

    # ---- Node
    package = read_json(root / "package.json")
    if package is not None:
        declared = str(package.get("packageManager") or "").split("@", 1)[0]
        lockfiles = {
            "pnpm": "pnpm-lock.yaml",
            "yarn": "yarn.lock",
            "bun": "bun.lockb",
            "npm": "package-lock.json",
        }
        chosen = None
        evidence = []
        if declared in lockfiles:
            chosen, evidence = declared, [f"packageManager: {package['packageManager']}"]
        else:
            for name, lock in lockfiles.items():
                if (root / lock).is_file() or (name == "bun" and (root / "bun.lock").is_file()):
                    chosen, evidence = name, [lock]
                    break
        if chosen is None:
            chosen, evidence = "npm", ["package.json (default)"]
        found.append(
            Detection(chosen, "package_manager", evidence, details={"ecosystem": "node", "lockfile": lockfiles[chosen]})
        )

    # ---- Dart / Flutter
    if (root / "pubspec.yaml").is_file():
        from highhx.project.manifest import read_yaml_mapping

        pubspec = read_yaml_mapping(root / "pubspec.yaml") or {}
        flutter = "flutter" in (pubspec.get("dependencies") or {})
        found.append(
            Detection(
                "flutter" if flutter else "dart",
                "package_manager",
                ["pubspec.yaml"],
                details={"ecosystem": "dart", "lockfile": "pubspec.lock"},
            )
        )

    # ---- JVM
    if (root / "pom.xml").is_file():
        wrapper = (root / executable_name("mvnw")).is_file() or (root / "mvnw").is_file()
        found.append(
            Detection("maven", "package_manager", ["pom.xml"], details={"ecosystem": "java", "wrapper": wrapper})
        )
    elif (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
        wrapper = (root / "gradlew").is_file() or (root / "gradlew.bat").is_file()
        found.append(
            Detection(
                "gradle", "package_manager", ["build.gradle(.kts)"], details={"ecosystem": "java", "wrapper": wrapper}
            )
        )

    # ---- Others
    if (root / "Cargo.toml").is_file():
        found.append(
            Detection(
                "cargo", "package_manager", ["Cargo.toml"], details={"ecosystem": "rust", "lockfile": "Cargo.lock"}
            )
        )
    if (root / "go.mod").is_file():
        found.append(Detection("go", "package_manager", ["go.mod"], details={"ecosystem": "go", "lockfile": "go.sum"}))
    if (root / "conanfile.txt").is_file() or (root / "conanfile.py").is_file():
        found.append(Detection("conan", "package_manager", ["conanfile"], details={"ecosystem": "cpp"}))
    elif (root / "vcpkg.json").is_file():
        found.append(Detection("vcpkg", "package_manager", ["vcpkg.json"], details={"ecosystem": "cpp"}))
    return found

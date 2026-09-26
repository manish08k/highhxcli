"""Language / stack detection from real project files."""

from __future__ import annotations

from pathlib import Path

from highhx.detection import Detection
from highhx.project.manifest import read_json, read_yaml_mapping
from highhx.utils.filesystem import read_text

PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "requirements.txt", "tox.ini")
JAVA_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts")
CPP_MARKERS = ("CMakeLists.txt", "meson.build", "conanfile.txt", "conanfile.py", "vcpkg.json")


def _present(root: Path, names: tuple[str, ...]) -> list[str]:
    return [name for name in names if (root / name).is_file()]


def _has_sources(
    root: Path, suffixes: tuple[str, ...], limit_dirs: tuple[str, ...] = ("src", ".", "lib", "include")
) -> bool:
    for directory in limit_dirs:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.iterdir():
            if path.is_file() and path.suffix in suffixes:
                return True
    return False


def detect_languages(root: Path) -> list[Detection]:
    """Detect languages/stacks at ``root`` (not recursive)."""
    found: list[Detection] = []

    python_evidence = _present(root, PYTHON_MARKERS)
    python_evidence += [p.name for p in sorted(root.glob("requirements*.txt")) if p.name not in python_evidence]
    if python_evidence:
        found.append(Detection("python", "language", python_evidence))

    if (root / "package.json").is_file():
        package = read_json(root / "package.json") or {}
        evidence = ["package.json"]
        typescript = (root / "tsconfig.json").is_file() or "typescript" in (package.get("devDependencies") or {})
        if typescript:
            evidence.append("tsconfig.json" if (root / "tsconfig.json").is_file() else "typescript dependency")
        found.append(Detection("node", "language", evidence, details={"typescript": bool(typescript)}))

    pubspec = read_yaml_mapping(root / "pubspec.yaml")
    if pubspec is not None:
        deps = pubspec.get("dependencies") or {}
        is_flutter = "flutter" in deps or "flutter" in (pubspec.get("environment") or {})
        found.append(
            Detection(
                "flutter" if is_flutter else "dart", "language", ["pubspec.yaml"], details={"flutter": is_flutter}
            )
        )

    java_evidence = _present(root, JAVA_MARKERS)
    if java_evidence:
        kotlin = (root / "build.gradle.kts").is_file() or (root / "src" / "main" / "kotlin").is_dir()
        found.append(Detection("java", "language", java_evidence, details={"kotlin": kotlin}))

    cpp_evidence = _present(root, CPP_MARKERS)
    if (
        not cpp_evidence
        and (root / "Makefile").is_file()
        and _has_sources(root, (".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"))
    ):
        cpp_evidence = ["Makefile", "C/C++ sources"]
    if cpp_evidence:
        found.append(Detection("cpp", "language", cpp_evidence))

    if (root / "go.mod").is_file():
        found.append(Detection("go", "language", ["go.mod"]))
    if (root / "Cargo.toml").is_file():
        found.append(Detection("rust", "language", ["Cargo.toml"]))

    dockerfiles = [p.name for p in sorted(root.glob("Dockerfile*")) if p.is_file()]
    if dockerfiles:
        found.append(Detection("docker", "language", dockerfiles))
    return found


def has_makefile_target(root: Path, target: str) -> bool:
    makefile = root / "Makefile"
    if not makefile.is_file():
        return False
    return any(line.startswith(f"{target}:") for line in read_text(makefile).splitlines())

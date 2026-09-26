"""Package manager adapters: which commands to run for each manager."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from highhx.project.detector import ProjectProfile
from highhx.utils.platform import IS_WINDOWS
from highhx.utils.processes import which


@dataclass
class ManagerAdapter:
    """Commands HighhX uses for one package manager."""

    name: str
    ecosystem: str
    executable: str
    install: list[str]
    update: list[str] | None = None
    update_packages: list[str] | None = None
    """argv prefix; package names are appended."""
    outdated: list[str] | None = None
    outdated_format: str | None = None
    audit: list[str] | None = None
    audit_format: str | None = None
    lockfile: str | None = None
    clean_paths: list[str] = field(default_factory=list)
    notes: str = ""

    @property
    def available(self) -> bool:
        exe = self.executable
        if exe.startswith("./"):
            return True
        return which(exe) is not None


def _python_exe() -> str:
    return "python" if IS_WINDOWS or which("python") else "python3"


def adapter_for(name: str, root: Path) -> ManagerAdapter | None:
    py = _python_exe()
    if name == "pip":
        req = root / "requirements.txt"
        install = (
            [py, "-m", "pip", "install", "-r", "requirements.txt"]
            if req.is_file()
            else [py, "-m", "pip", "install", "-e", "."]
        )
        return ManagerAdapter(
            "pip",
            "python",
            py,
            install,
            update=None,
            update_packages=[py, "-m", "pip", "install", "--upgrade"],
            outdated=[py, "-m", "pip", "list", "--outdated", "--format=json"],
            outdated_format="pip-json",
            audit=["pip-audit", "-f", "json"] + (["-r", "requirements.txt"] if req.is_file() else []),
            audit_format="pip-audit",
            clean_paths=[".venv"],
            notes="pip has no lockfile; `deps update` upgrades named packages only.",
        )
    if name == "uv":
        return ManagerAdapter(
            "uv",
            "python",
            "uv",
            ["uv", "sync"],
            update=["uv", "lock", "--upgrade"],
            update_packages=["uv", "lock", "--upgrade-package"],
            outdated=["uv", "pip", "list", "--outdated", "--format=json"],
            outdated_format="pip-json",
            audit=["pip-audit", "-f", "json"],
            audit_format="pip-audit",
            lockfile="uv.lock",
            clean_paths=[".venv"],
        )
    if name == "poetry":
        return ManagerAdapter(
            "poetry",
            "python",
            "poetry",
            ["poetry", "install"],
            update=["poetry", "update"],
            update_packages=["poetry", "update"],
            outdated=["poetry", "show", "--outdated", "--no-ansi"],
            outdated_format="poetry-text",
            audit=["pip-audit", "-f", "json"],
            audit_format="pip-audit",
            lockfile="poetry.lock",
            clean_paths=[".venv"],
        )
    if name == "pdm":
        return ManagerAdapter(
            "pdm",
            "python",
            "pdm",
            ["pdm", "install"],
            update=["pdm", "update"],
            update_packages=["pdm", "update"],
            lockfile="pdm.lock",
            clean_paths=[".venv"],
        )
    if name == "pipenv":
        return ManagerAdapter(
            "pipenv",
            "python",
            "pipenv",
            ["pipenv", "install", "--dev"],
            update=["pipenv", "update"],
            update_packages=["pipenv", "update"],
            lockfile="Pipfile.lock",
        )
    if name == "npm":
        lock = (root / "package-lock.json").is_file()
        return ManagerAdapter(
            "npm",
            "node",
            "npm",
            ["npm", "ci"] if lock else ["npm", "install"],
            update=["npm", "update"],
            update_packages=["npm", "update"],
            outdated=["npm", "outdated", "--json"],
            outdated_format="npm-json",
            audit=["npm", "audit", "--json"],
            audit_format="npm-audit",
            lockfile="package-lock.json",
            clean_paths=["node_modules"],
        )
    if name == "pnpm":
        return ManagerAdapter(
            "pnpm",
            "node",
            "pnpm",
            ["pnpm", "install"],
            update=["pnpm", "update"],
            update_packages=["pnpm", "update"],
            outdated=["pnpm", "outdated", "--format", "json"],
            outdated_format="npm-json",
            audit=["pnpm", "audit", "--json"],
            audit_format="npm-audit",
            lockfile="pnpm-lock.yaml",
            clean_paths=["node_modules"],
        )
    if name == "yarn":
        return ManagerAdapter(
            "yarn",
            "node",
            "yarn",
            ["yarn", "install"],
            update=["yarn", "upgrade"],
            update_packages=["yarn", "upgrade"],
            outdated=["yarn", "outdated", "--json"],
            outdated_format="yarn-json",
            audit=["yarn", "audit", "--json"],
            audit_format="yarn-audit",
            lockfile="yarn.lock",
            clean_paths=["node_modules"],
        )
    if name == "bun":
        return ManagerAdapter(
            "bun",
            "node",
            "bun",
            ["bun", "install"],
            update=["bun", "update"],
            update_packages=["bun", "update"],
            lockfile="bun.lockb",
            clean_paths=["node_modules"],
        )
    if name in ("flutter", "dart"):
        return ManagerAdapter(
            name,
            "dart",
            name,
            [name, "pub", "get"],
            update=[name, "pub", "upgrade"],
            update_packages=[name, "pub", "upgrade"],
            outdated=[name, "pub", "outdated", "--json"],
            outdated_format="pub-json",
            lockfile="pubspec.lock",
            clean_paths=[".dart_tool"],
        )
    if name == "maven":
        wrapper = "mvnw.cmd" if IS_WINDOWS else "./mvnw"
        exe = wrapper if (root / wrapper.lstrip("./")).is_file() else "mvn"
        return ManagerAdapter(
            "maven",
            "java",
            exe,
            [exe, "dependency:resolve"],
            outdated=[exe, "-q", "versions:display-dependency-updates"],
            outdated_format="maven-text",
            clean_paths=["target"],
            notes="Maven has no lockfile; `deps update` is not automated (edit pom.xml versions).",
        )
    if name == "gradle":
        wrapper = "gradlew.bat" if IS_WINDOWS else "./gradlew"
        exe = wrapper if (root / wrapper.lstrip("./")).is_file() else "gradle"
        return ManagerAdapter(
            "gradle",
            "java",
            exe,
            [exe, "dependencies"],
            clean_paths=["build", ".gradle"],
            notes="Gradle dependency updates require a versions plugin.",
        )
    if name == "cargo":
        return ManagerAdapter(
            "cargo",
            "rust",
            "cargo",
            ["cargo", "fetch"],
            update=["cargo", "update"],
            update_packages=["cargo", "update", "-p"],
            audit=["cargo", "audit", "--json"],
            audit_format="cargo-audit",
            lockfile="Cargo.lock",
            clean_paths=["target"],
        )
    if name == "go":
        return ManagerAdapter(
            "go",
            "go",
            "go",
            ["go", "mod", "download"],
            update=["go", "get", "-u", "./..."],
            update_packages=["go", "get", "-u"],
            outdated=["go", "list", "-u", "-m", "-json", "all"],
            outdated_format="go-json",
            lockfile="go.sum",
        )
    return None


def detect_adapters(root: Path, profile: ProjectProfile) -> list[ManagerAdapter]:
    adapters: list[ManagerAdapter] = []
    for detection in profile.package_managers:
        adapter = adapter_for(detection.name, root)
        if adapter is not None:
            adapters.append(adapter)
    return adapters

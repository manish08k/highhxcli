"""Project detection: combines all detectors into a :class:`ProjectProfile`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from highhx.detection import Detection
from highhx.detection.container import detect_containers, find_compose_files
from highhx.detection.database import detect_databases
from highhx.detection.framework import detect_frameworks
from highhx.detection.language import detect_languages, has_makefile_target
from highhx.detection.package_manager import detect_package_managers
from highhx.project.manifest import ProjectManifest, load_manifests, manifest_problems, read_toml
from highhx.project.monorepo import MonorepoInfo, detect_monorepo
from highhx.utils.platform import IS_WINDOWS
from highhx.utils.processes import which

STACK_PRIORITY = ("flutter", "python", "nextjs", "react", "node", "java", "go", "rust", "cpp", "dart", "docker")
TEMPLATE_STACKS = ("python", "node", "react", "nextjs", "flutter", "java", "cpp", "docker")


@dataclass
class ProjectProfile:
    """Everything HighhX learned about a project by inspecting its files."""

    root: Path
    name: str
    languages: list[Detection] = field(default_factory=list)
    frameworks: list[Detection] = field(default_factory=list)
    package_managers: list[Detection] = field(default_factory=list)
    databases: list[Detection] = field(default_factory=list)
    containers: list[Detection] = field(default_factory=list)
    manifests: list[ProjectManifest] = field(default_factory=list)
    monorepo: MonorepoInfo | None = None
    git: bool = False
    stacks: list[str] = field(default_factory=list)
    primary: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    """Manifests that exist but could not be parsed."""

    def has(self, name: str) -> bool:
        return any(
            d.name == name
            for d in (*self.languages, *self.frameworks, *self.package_managers, *self.databases, *self.containers)
        )

    def package_manager(self, ecosystem: str) -> str | None:
        for detection in self.package_managers:
            if detection.details.get("ecosystem") == ecosystem:
                return detection.name
        return None

    @property
    def version(self) -> str | None:
        return next((m.version for m in self.manifests if m.version), None)

    @property
    def template(self) -> str:
        if self.monorepo is not None:
            return "monorepo"
        return self.primary if self.primary in TEMPLATE_STACKS else "generic"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "name": self.name,
            "primary": self.primary,
            "stacks": self.stacks,
            "version": self.version,
            "git": self.git,
            "languages": [d.to_dict() for d in self.languages],
            "frameworks": [d.to_dict() for d in self.frameworks],
            "package_managers": [d.to_dict() for d in self.package_managers],
            "databases": [d.to_dict() for d in self.databases],
            "containers": [d.to_dict() for d in self.containers],
            "monorepo": self.monorepo.to_dict() if self.monorepo else None,
            "commands": self.commands,
            "problems": self.problems,
        }


def _stacks(languages: list[Detection], frameworks: list[Detection]) -> list[str]:
    names = {d.name for d in languages}
    fw = {d.name for d in frameworks}
    stacks = set(names)
    if "node" in names and "nextjs" in fw:
        stacks.add("nextjs")
    if "node" in names and "react" in fw:
        stacks.add("react")
    return [s for s in STACK_PRIORITY if s in stacks]


def _python_cmd() -> str:
    if IS_WINDOWS:
        return "python"
    return "python" if which("python") else "python3"


def _python_commands(root: Path, profile: ProjectProfile) -> dict[str, str]:
    pm = profile.package_manager("python") or "pip"
    py = _python_cmd()
    prefix = {"uv": "uv run ", "poetry": "poetry run ", "pdm": "pdm run ", "pipenv": "pipenv run "}.get(pm, "")
    deps: set[str] = set()
    for manifest in profile.manifests:
        if manifest.source in ("pyproject.toml", "requirements.txt", "setup.py"):
            deps |= manifest.dependencies
    tool = (read_toml(root / "pyproject.toml") or {}).get("tool") or {}
    cmds: dict[str, str] = {}
    has_tests = (root / "tests").is_dir() or (root / "test").is_dir() or any(root.glob("test_*.py"))
    if "pytest" in deps or "pytest" in tool or (root / "pytest.ini").is_file() or (root / "conftest.py").is_file():
        cmds["test"] = f"{prefix}pytest" if prefix else f"{py} -m pytest"
    elif has_tests:
        cmds["test"] = f"{prefix}{py} -m unittest discover"
    if "ruff" in deps or "ruff" in tool or (root / "ruff.toml").is_file():
        cmds["lint"] = f"{prefix}ruff check ."
        cmds["format"] = f"{prefix}ruff format ."
        cmds["fix"] = f"{prefix}ruff check --fix ."
    elif "flake8" in deps:
        cmds["lint"] = f"{prefix}flake8"
    if "black" in deps and "format" not in cmds:
        cmds["format"] = f"{prefix}black ."
    if "mypy" in deps or "mypy" in tool:
        cmds["typecheck"] = f"{prefix}mypy" + ("" if (tool.get("mypy") or {}).get("files") else " .")
    cmds["install"] = {
        "uv": "uv sync",
        "poetry": "poetry install",
        "pdm": "pdm install",
        "pipenv": "pipenv install --dev",
    }.get(
        pm,
        f"{py} -m pip install -r requirements.txt"
        if (root / "requirements.txt").is_file()
        else f"{py} -m pip install -e .",
    )
    if (root / "pyproject.toml").is_file():
        cmds["build"] = {"uv": "uv build", "poetry": "poetry build", "pdm": "pdm build"}.get(pm, f"{py} -m build")
    if (root / "manage.py").is_file():
        cmds["dev"] = f"{prefix}{py} manage.py runserver"
    elif "fastapi" in deps:
        module = (
            "app.main" if (root / "app" / "main.py").is_file() else "main" if (root / "main.py").is_file() else None
        )
        if module:
            cmds["dev"] = f"{prefix}uvicorn {module}:app --reload"
            cmds["start"] = f"{prefix}uvicorn {module}:app"
    elif "flask" in deps:
        cmds["dev"] = f"{prefix}flask run --debug"
        cmds["start"] = f"{prefix}flask run"
    elif "streamlit" in deps:
        entry = next((n for n in ("app.py", "main.py", "streamlit_app.py") if (root / n).is_file()), None)
        if entry:
            cmds["dev"] = f"{prefix}streamlit run {entry}"
    elif (root / "main.py").is_file():
        cmds["dev"] = f"{prefix}{py} main.py"
    return cmds


def _node_commands(root: Path, profile: ProjectProfile) -> dict[str, str]:
    pm = profile.package_manager("node") or "npm"
    manifest = next((m for m in profile.manifests if m.source == "package.json"), None)
    scripts = manifest.scripts if manifest else {}

    def run(script: str) -> str:
        if pm == "npm":
            return "npm test" if script == "test" else f"npm run {script}"
        if pm == "yarn":
            return f"yarn {script}"
        return f"{pm} run {script}"

    cmds: dict[str, str] = {}
    for name, candidates in {
        "dev": ("dev", "start:dev", "serve"),
        "start": ("start",),
        "test": ("test",),
        "build": ("build",),
        "lint": ("lint",),
        "format": ("format", "fmt", "prettier"),
        "fix": ("lint:fix", "fix"),
        "typecheck": ("typecheck", "type-check", "tsc", "types"),
    }.items():
        for script in candidates:
            body = scripts.get(script)
            if body and "no test specified" not in body:
                cmds[name] = run(script)
                break
    if "dev" not in cmds and "start" in cmds:
        cmds["dev"] = cmds["start"]
    lock_present = (root / "package-lock.json").is_file()
    cmds["install"] = {
        "npm": "npm ci" if lock_present else "npm install",
        "pnpm": "pnpm install",
        "yarn": "yarn install",
        "bun": "bun install",
    }.get(pm, "npm install")
    return cmds


def _flutter_commands(root: Path, flutter: bool) -> dict[str, str]:
    tool = "flutter" if flutter else "dart"
    cmds = {
        "test": f"{tool} test",
        "lint": f"{tool} analyze",
        "format": "dart format .",
        "fix": "dart fix --apply",
        "install": f"{tool} pub get",
    }
    if flutter:
        cmds["dev"] = "flutter run"
        cmds["clean"] = "flutter clean"
        if (root / "web").is_dir():
            cmds["build"] = "flutter build web"
        elif (root / "android").is_dir():
            cmds["build"] = "flutter build apk"
        elif (root / "ios").is_dir():
            cmds["build"] = "flutter build ios --no-codesign"
    else:
        cmds["dev"] = "dart run"
        if (root / "bin").is_dir():
            main = next(iter(sorted((root / "bin").glob("*.dart"))), None)
            if main is not None:
                cmds["build"] = f"dart compile exe bin/{main.name} -o build/{main.stem}"
    return cmds


def _java_commands(root: Path, profile: ProjectProfile) -> dict[str, str]:
    spring = profile.has("spring-boot")
    if profile.package_manager("java") == "maven":
        wrapper = root / ("mvnw.cmd" if IS_WINDOWS else "mvnw")
        mvn = (wrapper.name if IS_WINDOWS else "./mvnw") if wrapper.is_file() else "mvn"
        cmds = {
            "test": f"{mvn} test",
            "build": f"{mvn} package",
            "install": f"{mvn} dependency:resolve",
            "clean": f"{mvn} clean",
            "package": f"{mvn} package -DskipTests",
        }
        if spring:
            cmds["dev"] = f"{mvn} spring-boot:run"
        return cmds
    wrapper = root / ("gradlew.bat" if IS_WINDOWS else "gradlew")
    gradle = (wrapper.name if IS_WINDOWS else "./gradlew") if wrapper.is_file() else "gradle"
    cmds = {
        "test": f"{gradle} test",
        "build": f"{gradle} build",
        "install": f"{gradle} dependencies",
        "clean": f"{gradle} clean",
        "package": f"{gradle} assemble",
    }
    if spring:
        cmds["dev"] = f"{gradle} bootRun"
    return cmds


def _cpp_commands(root: Path) -> dict[str, str]:
    if (root / "CMakeLists.txt").is_file():
        return {
            "build": "cmake -S . -B build && cmake --build build",
            "test": "ctest --test-dir build --output-on-failure",
            "install": "cmake -S . -B build",
        }
    if (root / "meson.build").is_file():
        return {"build": "meson setup builddir && meson compile -C builddir", "test": "meson test -C builddir"}
    cmds = {"build": "make"}
    if has_makefile_target(root, "test"):
        cmds["test"] = "make test"
    if has_makefile_target(root, "clean"):
        cmds["clean"] = "make clean"
    return cmds


def suggest_commands(root: Path, profile: ProjectProfile) -> dict[str, str]:
    """Best-effort commands for the primary stack, based only on real files."""
    primary = profile.primary
    cmds: dict[str, str] = {}
    if primary == "python":
        cmds = _python_commands(root, profile)
    elif primary in ("node", "react", "nextjs"):
        cmds = _node_commands(root, profile)
    elif primary in ("flutter", "dart"):
        cmds = _flutter_commands(root, primary == "flutter")
    elif primary == "java":
        cmds = _java_commands(root, profile)
    elif primary == "cpp":
        cmds = _cpp_commands(root)
    elif primary == "go":
        cmds = {
            "build": "go build ./...",
            "test": "go test ./...",
            "lint": "go vet ./...",
            "format": "gofmt -w .",
            "install": "go mod download",
            "dev": "go run .",
        }
    elif primary == "rust":
        cmds = {
            "build": "cargo build",
            "test": "cargo test",
            "lint": "cargo clippy",
            "format": "cargo fmt",
            "install": "cargo fetch",
            "dev": "cargo run",
        }
    if profile.has("docker-compose") and "dev" not in cmds:
        cmds["dev"] = "docker compose up"
    if primary == "docker" or (profile.has("dockerfile") and "package" not in cmds and primary is None):
        cmds.setdefault("build", f"docker build -t {profile.name.lower()} .")
    return cmds


def detect_project(root: Path) -> ProjectProfile:
    """Inspect ``root`` and return a :class:`ProjectProfile`."""
    root = root.resolve()
    languages = detect_languages(root)
    frameworks = detect_frameworks(root)
    manifests = load_manifests(root)
    name = next((m.name for m in manifests if m.name), None) or root.name
    containers = detect_containers(root)
    profile = ProjectProfile(
        root=root,
        name=str(name),
        languages=languages,
        frameworks=frameworks,
        package_managers=detect_package_managers(root),
        databases=detect_databases(root, find_compose_files(root)),
        containers=containers,
        manifests=manifests,
        monorepo=detect_monorepo(root),
        git=(root / ".git").exists(),
    )
    profile.stacks = _stacks(languages, frameworks)
    profile.primary = profile.stacks[0] if profile.stacks else None
    profile.commands = suggest_commands(root, profile)
    profile.problems = manifest_problems(root)
    return profile

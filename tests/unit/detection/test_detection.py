import json
from pathlib import Path

from highhx.detection.runtime import satisfies_minimum
from highhx.detection.tools import parse_version
from highhx.project.detector import detect_project
from highhx.project.monorepo import detect_monorepo
from tests.conftest import FIXTURES, copy_fixture


def write(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def names(items) -> set[str]:  # type: ignore[no-untyped-def]
    return {d.name for d in items}


def test_python_project(tmp_path: Path) -> None:
    profile = detect_project(copy_fixture("python_project", tmp_path))
    assert profile.primary == "python" and profile.name == "pyapp" and profile.version == "1.2.3"
    assert "pytest" in names(profile.frameworks)
    assert "pytest" in profile.commands["test"]


def test_poetry_and_uv_are_distinguished(tmp_path: Path) -> None:
    poetry = write(
        tmp_path / "p",
        {
            "pyproject.toml": "[tool.poetry]\nname='x'\nversion='1.0.0'\n[tool.poetry.dependencies]\npython='^3.11'\nfastapi='*'\n",
            "poetry.lock": "",
            "app/main.py": "",
        },
    )
    profile = detect_project(poetry)
    assert profile.package_manager("python") == "poetry"
    assert profile.commands["install"] == "poetry install"
    assert profile.commands["dev"].startswith("poetry run uvicorn app.main:app")
    uv = write(
        tmp_path / "u",
        {"pyproject.toml": "[project]\nname='y'\nversion='0.1.0'\ndependencies=['ruff']\n", "uv.lock": ""},
    )
    profile = detect_project(uv)
    assert profile.package_manager("python") == "uv" and profile.commands["lint"] == "uv run ruff check ."


def test_node_nextjs_project(tmp_path: Path) -> None:
    profile = detect_project(copy_fixture("node_project", tmp_path))
    assert profile.primary == "nextjs"
    assert {"nextjs", "react", "vitest"} <= names(profile.frameworks)
    assert profile.package_manager("node") == "pnpm"
    assert profile.commands["test"] == "pnpm run test"
    assert "postgresql" in names(profile.databases)
    assert profile.languages[0].details["typescript"] is True


def test_react_with_yarn(tmp_path: Path) -> None:
    root = write(
        tmp_path,
        {
            "package.json": json.dumps(
                {"name": "r", "scripts": {"start": "vite", "test": "jest"}, "dependencies": {"react": "18"}}
            ),
            "yarn.lock": "",
        },
    )
    profile = detect_project(root)
    assert profile.primary == "react" and profile.package_manager("node") == "yarn"
    assert profile.commands["dev"] == "yarn start"


def test_flutter_project(tmp_path: Path) -> None:
    root = write(
        tmp_path,
        {
            "pubspec.yaml": "name: app\nversion: 1.0.0+1\ndependencies:\n  flutter:\n    sdk: flutter\n",
            "android/.keep": "",
        },
    )
    profile = detect_project(root)
    assert profile.primary == "flutter"
    assert profile.commands["build"] == "flutter build apk"
    assert profile.commands["install"] == "flutter pub get"


def test_java_maven_spring(tmp_path: Path) -> None:
    pom = """<project xmlns="http://maven.apache.org/POM/4.0.0"><artifactId>svc</artifactId><version>2.0.0</version>
    <parent><artifactId>spring-boot-starter-parent</artifactId></parent>
    <dependencies><dependency><artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>"""
    profile = detect_project(write(tmp_path, {"pom.xml": pom}))
    assert profile.primary == "java" and profile.name == "svc" and profile.version == "2.0.0"
    assert "spring-boot" in names(profile.frameworks)
    assert profile.commands["dev"].endswith("spring-boot:run")


def test_gradle_wrapper(tmp_path: Path) -> None:
    profile = detect_project(
        write(
            tmp_path,
            {
                "build.gradle.kts": 'version = "0.3.0"\n',
                "gradlew": "",
                "gradlew.bat": "",
                "settings.gradle.kts": 'rootProject.name = "g"\n',
            },
        )
    )
    assert profile.package_manager("java") == "gradle"
    assert "gradlew" in profile.commands["test"]
    assert profile.name == "g"


def test_cmake_project(tmp_path: Path) -> None:
    profile = detect_project(write(tmp_path, {"CMakeLists.txt": "project(engine VERSION 3.1.0)\n"}))
    assert profile.primary == "cpp" and profile.version == "3.1.0"
    assert profile.commands["build"].startswith("cmake -S . -B build")


def test_docker_project_with_database(tmp_path: Path) -> None:
    profile = detect_project(copy_fixture("docker_project", tmp_path))
    assert {"dockerfile", "docker-compose"} <= names(profile.containers)
    assert "postgresql" in names(profile.databases)
    assert profile.primary == "docker"


def test_monorepo_detection(tmp_path: Path) -> None:
    root = copy_fixture("monorepo", tmp_path)
    info = detect_monorepo(root)
    assert info is not None and info.tool == "npm-workspaces"
    assert info.members == ["packages/api", "packages/web"]
    assert detect_project(root).template == "monorepo"


def test_pnpm_workspace_and_directory_layout(tmp_path: Path) -> None:
    pnpm = write(
        tmp_path / "a",
        {
            "pnpm-workspace.yaml": "packages:\n  - 'apps/*'\n",
            "apps/one/package.json": "{}",
            "apps/two/package.json": "{}",
        },
    )
    assert detect_monorepo(pnpm).members == ["apps/one", "apps/two"]  # type: ignore[union-attr]
    layout = write(
        tmp_path / "b", {"services/a/pyproject.toml": "[project]\nname='a'\n", "services/b/go.mod": "module b\n"}
    )
    assert detect_monorepo(layout).tool == "directory-layout"  # type: ignore[union-attr]


def test_empty_directory(tmp_path: Path) -> None:
    profile = detect_project(tmp_path)
    assert profile.primary is None and profile.template == "generic" and profile.commands == {}


def test_version_helpers() -> None:
    assert parse_version("git version 2.43.0") == "2.43.0"
    assert parse_version('openjdk version "21.0.2" 2024-01-16') == "21.0.2"
    assert satisfies_minimum("3.12.1", ">=3.11") is True
    assert satisfies_minimum("3.10.0", ">=3.11") is False
    assert satisfies_minimum("20.11.0", "^20") is True
    assert satisfies_minimum("18.0.0", ">=18 <21") is True
    assert satisfies_minimum("1.0", "~=1.0 || 2") is None


def test_fixture_dirs_exist() -> None:
    assert (FIXTURES / "python_project" / "pyproject.toml").exists()

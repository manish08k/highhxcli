"""Framework detection from dependencies and configuration files."""

from __future__ import annotations

from pathlib import Path

from highhx.detection import Detection
from highhx.project.manifest import node_dependencies, python_dependencies, read_json, read_pom

NODE_FRAMEWORKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # name, dependency, config files
    ("nextjs", "next", ("next.config.js", "next.config.mjs", "next.config.ts")),
    ("react", "react", ()),
    ("vue", "vue", ("vue.config.js",)),
    ("nuxt", "nuxt", ("nuxt.config.ts", "nuxt.config.js")),
    ("angular", "@angular/core", ("angular.json",)),
    ("svelte", "svelte", ("svelte.config.js",)),
    ("vite", "vite", ("vite.config.ts", "vite.config.js", "vite.config.mjs")),
    ("express", "express", ()),
    ("nestjs", "@nestjs/core", ("nest-cli.json",)),
    ("jest", "jest", ("jest.config.js", "jest.config.ts", "jest.config.cjs", "jest.config.mjs")),
    ("vitest", "vitest", ("vitest.config.ts", "vitest.config.js", "vitest.config.mts")),
)

PYTHON_FRAMEWORKS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("django", "django", ("manage.py",)),
    ("flask", "flask", ()),
    ("fastapi", "fastapi", ()),
    ("pytest", "pytest", ("pytest.ini", "conftest.py")),
    ("sqlalchemy", "sqlalchemy", ()),
    ("alembic", "alembic", ("alembic.ini",)),
    ("celery", "celery", ()),
    ("streamlit", "streamlit", ()),
)


def detect_frameworks(root: Path) -> list[Detection]:
    found: list[Detection] = []
    package = read_json(root / "package.json")
    if package is not None:
        deps = node_dependencies(package)
        for name, dep, files in NODE_FRAMEWORKS:
            evidence = [f"dependency {dep}"] if dep in deps else []
            evidence += [f for f in files if (root / f).is_file()]
            if evidence:
                found.append(Detection(name, "framework", evidence))
    py_markers = ("pyproject.toml", "setup.py", "Pipfile", "requirements.txt")
    if any((root / m).is_file() for m in py_markers) or list(root.glob("requirements*.txt")):
        deps = python_dependencies(root)
        for name, dep, files in PYTHON_FRAMEWORKS:
            evidence = [f"dependency {dep}"] if dep in deps else []
            evidence += [f for f in files if (root / f).is_file()]
            if evidence:
                found.append(Detection(name, "framework", evidence))
    pom = read_pom(root / "pom.xml")
    gradle_text = ""
    for gradle in ("build.gradle.kts", "build.gradle"):
        if (root / gradle).is_file():
            gradle_text = (root / gradle).read_text(encoding="utf-8", errors="replace")
    if (
        (pom and any("spring-boot" in d for d in pom.get("dependencies") or []))
        or (pom and "spring-boot" in str(pom.get("parent", "")))
        or "org.springframework.boot" in gradle_text
    ):
        found.append(Detection("spring-boot", "framework", ["spring-boot dependency"]))
    return found

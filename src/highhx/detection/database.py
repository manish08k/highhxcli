"""Database technology detection."""

from __future__ import annotations

import re
from pathlib import Path

from highhx.detection import Detection
from highhx.project.manifest import node_dependencies, python_dependencies, read_json
from highhx.utils.filesystem import read_text

IMAGE_HINTS = {
    "postgres": "postgresql",
    "postgis": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mongo": "mongodb",
    "redis": "redis",
}
PY_HINTS = {
    "psycopg": "postgresql",
    "psycopg2": "postgresql",
    "psycopg2-binary": "postgresql",
    "asyncpg": "postgresql",
    "mysqlclient": "mysql",
    "pymysql": "mysql",
    "aiomysql": "mysql",
    "mysql-connector-python": "mysql",
    "pymongo": "mongodb",
    "motor": "mongodb",
    "redis": "redis",
    "aiosqlite": "sqlite",
}
NODE_HINTS = {
    "pg": "postgresql",
    "postgres": "postgresql",
    "mysql": "mysql",
    "mysql2": "mysql",
    "mongodb": "mongodb",
    "mongoose": "mongodb",
    "redis": "redis",
    "ioredis": "redis",
    "sqlite3": "sqlite",
    "better-sqlite3": "sqlite",
}
URL_SCHEMES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "sqlite": "sqlite",
    "mongodb": "mongodb",
    "redis": "redis",
}


def detect_databases(root: Path, compose_files: list[Path] | None = None) -> list[Detection]:
    evidence: dict[str, list[str]] = {}

    def add(name: str, why: str) -> None:
        evidence.setdefault(name, [])
        if why not in evidence[name]:
            evidence[name].append(why)

    for compose in compose_files or []:
        text = read_text(compose)
        for image in re.findall(r"image:\s*['\"]?([\w./-]+)", text):
            base = image.rsplit("/", 1)[-1].split(":", 1)[0]
            for hint, db in IMAGE_HINTS.items():
                if base.startswith(hint):
                    add(db, f"{compose.name}: image {image}")
    for dep in python_dependencies(root):
        if dep in PY_HINTS:
            add(PY_HINTS[dep], f"python dependency {dep}")
    for dep in node_dependencies(read_json(root / "package.json")):
        if dep in NODE_HINTS:
            add(NODE_HINTS[dep], f"node dependency {dep}")
    prisma = root / "prisma" / "schema.prisma"
    if prisma.is_file():
        match = re.search(r'provider\s*=\s*"(postgresql|mysql|sqlite|mongodb)"', read_text(prisma))
        if match:
            add(match.group(1), "prisma/schema.prisma")
    for env_file in (".env.example", ".env.sample", ".env"):
        path = root / env_file
        if path.is_file():
            for line in read_text(path).splitlines():
                m = re.match(r"\s*(?:export\s+)?\w*DATABASE_URL\w*\s*=\s*['\"]?([a-z0-9+]+)://", line, re.IGNORECASE)
                if m:
                    scheme = m.group(1).split("+", 1)[0].lower()
                    if scheme in URL_SCHEMES:
                        add(URL_SCHEMES[scheme], f"{env_file}: DATABASE_URL scheme")
    for pattern in ("*.sqlite", "*.sqlite3", "db.sqlite3"):
        for path in root.glob(pattern):
            add("sqlite", path.name)
    return [Detection(name, "database", why) for name, why in sorted(evidence.items())]

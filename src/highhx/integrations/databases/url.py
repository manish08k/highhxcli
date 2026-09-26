"""Database URL parsing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from highhx.core.errors import ValidationError

KINDS = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pgsql": "postgresql",
    "mysql": "mysql",
    "mariadb": "mysql",
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
}


@dataclass(frozen=True)
class DatabaseURL:
    scheme: str
    kind: str
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    database: str | None = None
    path: str | None = None
    params: tuple[tuple[str, str], ...] = ()

    def masked(self) -> str:
        """Display form without the password."""
        if self.kind == "sqlite":
            return f"sqlite:///{self.path}"
        auth = f"{self.user}:****@" if self.user and self.password else f"{self.user}@" if self.user else ""
        port = f":{self.port}" if self.port else ""
        return f"{self.kind}://{auth}{self.host or 'localhost'}{port}/{self.database or ''}"

    def sqlite_path(self, root: Path) -> Path | None:
        if self.kind != "sqlite" or not self.path or self.path == ":memory:":
            return None
        path = Path(self.path)
        return path if path.is_absolute() else root / path


def parse_database_url(url: str) -> DatabaseURL:
    """Parse ``postgresql://user:pass@host:5432/db``, ``mysql://…``, ``sqlite:///file.db``."""
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    base = scheme.split("+", 1)[0]
    kind = KINDS.get(base)
    if not kind:
        raise ValidationError(
            f"Unsupported database URL scheme '{scheme or '(none)'}'.",
            hint="Use postgresql://, mysql:// or sqlite:///path.db",
        )
    if kind == "sqlite":
        raw = url.split("://", 1)[1] if "://" in url else ""
        # sqlite:///relative.db -> "/relative.db"; sqlite:////abs.db -> "//abs.db"
        path = raw[1:] if raw.startswith("/") else raw
        path = path.split("?", 1)[0]
        return DatabaseURL(scheme=scheme, kind=kind, path=unquote(path) or ":memory:")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError(f"Invalid port in database URL: {exc}") from exc
    return DatabaseURL(
        scheme=scheme,
        kind=kind,
        host=parsed.hostname,
        port=port,
        user=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        database=parsed.path.lstrip("/") or None,
        params=tuple((k, v[0]) for k, v in parse_qs(parsed.query).items()),
    )

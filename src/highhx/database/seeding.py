"""Seed data files."""

from __future__ import annotations

from pathlib import Path


def seed_files(root: Path, configured: str | None) -> list[Path]:
    directory = (
        root / configured
        if configured
        else next((root / d for d in ("seeds", "db/seeds", "database/seeds") if (root / d).is_dir()), root / "seeds")
    )
    if not directory.is_dir():
        return []
    return sorted(p for p in directory.glob("*.sql") if p.is_file())

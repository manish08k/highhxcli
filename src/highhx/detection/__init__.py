"""Detectors that inspect real project files and the local machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Detection:
    """Something detected in a project, with the evidence that led to it."""

    name: str
    kind: str
    evidence: list[str] = field(default_factory=list)
    path: str = "."
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "evidence": self.evidence,
            "path": self.path,
            "details": self.details,
        }


__all__ = ["Detection"]

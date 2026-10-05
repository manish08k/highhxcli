"""Task artifacts: the files computer-use work produces — screenshots, downloads, generated and
parsed files — with an id, metadata and a lifecycle. (Build outputs in dist/ are a separate thing:
``highhx artifacts``.)

    <state>/artifacts/objects/<sha256>      the content, stored once (content-addressed), 0600
    <state>/artifacts/index.jsonl           one record per artifact: id, name, MIME type, size,
                                            checksum, kind, task/trace, the action that made it,
                                            created, retention

Secure access: the store is owner-only; content is read only by id, and its checksum is verified
on every read. Artifacts never enter a model's context by themselves (records carry metadata,
not contents). ``artifact.created`` is emitted for each one. Retention: ``prune()`` removes
artifacts older than their retention (default 30 days) and objects nothing refers to any more.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import mimetypes
import os
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from highhx.core.errors import UsageError
from highhx.utils.hashing import new_id

MAX_ARTIFACT_BYTES = 200_000_000
DEFAULT_RETENTION_DAYS = 30
KINDS = ("screenshot", "download", "generated", "parsed", "recording", "other")


@dataclass(frozen=True)
class Artifact:
    id: str
    name: str
    mime: str
    size: int
    sha256: str
    kind: str
    created: float
    retention_days: int = DEFAULT_RETENTION_DAYS
    task_id: str = ""
    trace_id: str = ""
    action: str = ""
    """The action that produced it (``browser.screenshot`` …)."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArtifactStore:
    def __init__(self, root: Path, *, emit: Any = None) -> None:
        self.root = root
        self.objects = root / "objects"
        self.index = root / "index.jsonl"
        self.emit = emit
        self._lock = threading.Lock()

    @classmethod
    def for_app(cls, app: Any) -> ArtifactStore:
        return cls(app.paths.state_dir / "artifacts", emit=app.ctx.events.emit)

    def _records(self) -> builtins.list[Artifact]:
        try:
            lines = self.index.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(Artifact(**json.loads(line)))
            except (ValueError, TypeError):
                continue
        return out

    def _write(self, records: builtins.list[Artifact]) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.index.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for record in records:
                out.write(json.dumps(record.to_dict()) + "\n")
        tmp.replace(self.index)

    def add(
        self,
        source: Path,
        *,
        kind: str = "other",
        name: str = "",
        action: str = "",
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> Artifact:
        """Store a copy of ``source`` and record it."""
        if kind not in KINDS:
            raise UsageError(f"artifact kind must be one of {', '.join(KINDS)}")
        if not source.is_file():
            raise UsageError(f"{source} is not a file")
        size = source.stat().st_size
        if size > MAX_ARTIFACT_BYTES:
            raise UsageError(f"{source.name} is larger than {MAX_ARTIFACT_BYTES // 1_000_000} MB")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        sha = digest.hexdigest()
        from highhx.core.events import current_context

        context = current_context()
        with self._lock:
            self.objects.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            target = self.objects / sha
            if not target.exists():
                shutil.copyfile(source, target)
                target.chmod(0o600)
            label = name or source.name
            artifact = Artifact(
                new_id("art"),
                label,
                mimetypes.guess_type(label)[0] or "application/octet-stream",
                size,
                sha,
                kind,
                time.time(),
                retention_days,
                str(context.get("task_id") or ""),
                str(context.get("trace_id") or ""),
                action,
            )
            self._write([*self._records(), artifact])
        if self.emit is not None:
            self.emit(
                "artifact.created",
                artifact=artifact.id,
                name=artifact.name,
                kind=kind,
                size=size,
                mime=artifact.mime,
                action=action,
            )
        return artifact

    def list(self, *, task_id: str = "", kind: str = "") -> builtins.list[Artifact]:
        return [a for a in self._records() if (not task_id or a.task_id == task_id) and (not kind or a.kind == kind)]

    def get(self, artifact_id: str) -> Artifact:
        found = next((a for a in self._records() if a.id == artifact_id), None)
        if found is None:
            raise UsageError(f"No artifact {artifact_id!r}.")
        return found

    def read(self, artifact_id: str) -> bytes:
        artifact = self.get(artifact_id)
        data = (self.objects / artifact.sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise UsageError(f"Artifact {artifact_id} does not match its checksum (it was changed on disk).")
        return data

    def delete(self, artifact_id: str) -> None:
        with self._lock:
            records = self._records()
            remaining = [a for a in records if a.id != artifact_id]
            if len(remaining) == len(records):
                raise UsageError(f"No artifact {artifact_id!r}.")
            self._write(remaining)
            self._collect(remaining)

    def prune(self, *, now: float | None = None) -> builtins.list[str]:
        """Remove artifacts past their retention; returns their ids."""
        now = now if now is not None else time.time()
        with self._lock:
            records = self._records()
            keep = [a for a in records if now - a.created <= a.retention_days * 86400]
            removed = [a.id for a in records if a not in keep]
            if removed:
                self._write(keep)
                self._collect(keep)
        return removed

    def _collect(self, records: builtins.list[Artifact]) -> None:
        wanted = {a.sha256 for a in records}
        if self.objects.is_dir():
            for path in self.objects.iterdir():
                if path.name not in wanted:
                    path.unlink(missing_ok=True)


def record(ctx: Any, path: str | Path, *, kind: str, action: str) -> dict[str, Any] | None:
    """Register an action's output file as an artifact (best effort: a failure never fails the action)."""
    try:
        store = ArtifactStore.for_app(ctx.app)
        source = Path(path)
        if not source.is_absolute():
            source = ctx.app.root / source
        return store.add(source, kind=kind, action=action).to_dict()
    except Exception:
        return None

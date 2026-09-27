"""Syncing agent session metadata to the HighhX platform (best effort, never blocking).

Only metadata is sent — project name, title, provider, model, turn count, token
usage and status. Transcripts and code stay on the machine (the model provider
of course sees what the agent sends it).
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from highhx import __version__
from highhx.core.errors import HighhXError

if TYPE_CHECKING:
    from highhx.agent.context import ProjectContext
    from highhx.agent.history import SessionRecord
    from highhx.cloud.account import CloudAccount

log = logging.getLogger(__name__)


def project_fingerprint(context: ProjectContext) -> str:
    """Stable, non-reversible project identifier: the git remote if any, else the path."""
    source = context.remote or str(context.root)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


class SessionSync:
    def __init__(self, cloud: CloudAccount) -> None:
        self.cloud = cloud
        self.failed = False
        self.project_id: str | None = None

    def start(self, record: SessionRecord, context: ProjectContext) -> None:
        if record.remote_id or self.failed:
            return
        try:
            client = self.cloud.client()
            if self.project_id is None:
                project = client.post(
                    "/v1/projects",
                    {"name": context.name, "fingerprint": project_fingerprint(context), "stack": context.stack_label},
                )
                self.project_id = str(project.get("id") or "") or None
            created = client.post(
                "/v1/agent/sessions",
                {
                    "project_id": self.project_id,
                    "title": record.title,
                    "provider": record.provider,
                    "model": record.model,
                    "client_version": __version__,
                    "local_id": record.id,
                },
            )
            record.remote_id = str(created.get("id") or "") or None
        except HighhXError as exc:
            self.failed = True
            log.info("session sync disabled: %s", exc.message)

    def update(self, record: SessionRecord) -> None:
        if not record.remote_id or self.failed:
            return
        try:
            self.cloud.client().patch(
                f"/v1/agent/sessions/{record.remote_id}",
                {
                    "title": record.title,
                    "status": record.status,
                    "turns": record.turns,
                    "provider": record.provider,
                    "model": record.model,
                    "usage": record.usage.to_dict(),
                },
            )
        except HighhXError as exc:
            log.info("session sync failed: %s", exc.message)

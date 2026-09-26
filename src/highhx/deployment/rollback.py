"""Choosing what to roll back to."""

from __future__ import annotations

from highhx.core.errors import NotFoundError
from highhx.deployment.state import DeploymentRecord, DeploymentStore


def rollback_pair(
    store: DeploymentStore, target: str, to: str | None = None
) -> tuple[DeploymentRecord, DeploymentRecord]:
    """Return ``(current, previous)``: the active deployment and the one to restore."""
    successful = store.successful(target)
    if not successful:
        raise NotFoundError(f"No successful deployment of '{target}' is recorded.", hint="Nothing to roll back.")
    current = successful[0]
    if to:
        previous = store.get(to)
        if previous.target != target:
            raise NotFoundError(f"Deployment {to} belongs to target '{previous.target}', not '{target}'.")
        return current, previous
    for candidate in successful[1:]:
        if candidate.version != current.version or candidate.git_sha != current.git_sha:
            return current, candidate
    raise NotFoundError(
        f"No earlier successful deployment of '{target}' with a different version exists.",
        hint="Rollback needs at least two successful deployments with different versions.",
    )

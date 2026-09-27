"""Token expiry, suspensions, billing event ordering, idempotent metering, audit events

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("api_tokens") as batch:
        batch.add_column(sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("subscriptions") as batch:
        batch.add_column(sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("webhook_events") as batch:
        batch.add_column(sa.Column("created", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("outcome", sa.String(length=100), nullable=False, server_default=""))
    with op.batch_alter_table("usage_records") as batch:
        batch.add_column(sa.Column("request_id", sa.String(length=80), nullable=True))
        batch.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        batch.alter_column("session_id", existing_type=sa.String(length=32), type_=sa.String(length=64))
        batch.create_unique_constraint("uq_usage_records_request_id", ["request_id"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("subject_user_id", sa.String(length=32), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_subject_user_id", "audit_events", ["subject_user_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_subject_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    with op.batch_alter_table("usage_records") as batch:
        batch.drop_constraint("uq_usage_records_request_id", type_="unique")
        batch.alter_column("session_id", existing_type=sa.String(length=64), type_=sa.String(length=32))
        batch.drop_column("finished_at")
        batch.drop_column("request_id")
    with op.batch_alter_table("webhook_events") as batch:
        batch.drop_column("outcome")
        batch.drop_column("created")
    with op.batch_alter_table("subscriptions") as batch:
        batch.drop_column("last_event_at")
    with op.batch_alter_table("api_tokens") as batch:
        batch.drop_column("expires_at")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("suspended_at")

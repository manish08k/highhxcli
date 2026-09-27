"""Shared AI stream state and rate-limit counters (multi-instance deployments)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_streams",
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("owner", sa.String(length=120), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("done", sa.Boolean(), nullable=False),
        sa.Column("events_pruned", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reader_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index("ix_ai_streams_user_id", "ai_streams", ["user_id"])
    op.create_index("ix_ai_streams_finished_at", "ai_streams", ["finished_at"])
    op.create_table(
        "ai_stream_events",
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("frame", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["ai_streams.request_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("request_id", "seq"),
    )
    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(length=200), nullable=False),
        sa.Column("window_start", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_counters")
    op.drop_table("ai_stream_events")
    op.drop_index("ix_ai_streams_finished_at", table_name="ai_streams")
    op.drop_index("ix_ai_streams_user_id", table_name="ai_streams")
    op.drop_table("ai_streams")

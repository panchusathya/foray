"""org send gap and sending hours

The gap between automated emails is now per sending domain and editable per
org (send_gap_enabled / send_gap_seconds, null = deployment default), and an
org can restrict candidate email to working hours (send_window_*). Sending
hours start off, so nothing changes until someone turns them on.

Revision ID: c9e6a3b4d7f8
Revises: b8d5f2a3c6e7
Create Date: 2026-10-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c9e6a3b4d7f8"
down_revision = "b8d5f2a3c6e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orgs", sa.Column("send_gap_enabled", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("orgs", sa.Column("send_gap_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "orgs",
        sa.Column("send_window_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "orgs", sa.Column("send_window_start", sa.String(5), nullable=False, server_default="09:00")
    )
    op.add_column(
        "orgs", sa.Column("send_window_end", sa.String(5), nullable=False, server_default="17:00")
    )
    op.add_column(
        "orgs",
        sa.Column(
            "send_window_tz", sa.String(64), nullable=False, server_default="America/Los_Angeles"
        ),
    )
    op.add_column(
        "orgs",
        sa.Column("send_window_days", sa.JSON(), nullable=False, server_default="[0, 1, 2, 3, 4]"),
    )
    op.add_column(
        "orgs", sa.Column("send_window_breaks", sa.JSON(), nullable=False, server_default="[]")
    )


def downgrade() -> None:
    for column in (
        "send_window_breaks",
        "send_window_days",
        "send_window_tz",
        "send_window_end",
        "send_window_start",
        "send_window_enabled",
        "send_gap_seconds",
        "send_gap_enabled",
    ):
        op.drop_column("orgs", column)

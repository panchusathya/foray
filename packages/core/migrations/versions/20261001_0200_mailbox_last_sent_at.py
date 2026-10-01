"""mailbox last_sent_at

Each mailbox's own gap clock. The gap between automated emails moved from
one org-wide clock to one per mailbox, so mailboxes send side by side.

Revision ID: b8d5f2a3c6e7
Revises: a7c4e1f9b2d3
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b8d5f2a3c6e7"
down_revision = "a7c4e1f9b2d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mailbox_connections",
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mailbox_connections", "last_sent_at")

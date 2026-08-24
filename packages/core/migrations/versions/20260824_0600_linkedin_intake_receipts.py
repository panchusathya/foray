"""LinkedIn-first messaging flow + Linq delivery/read receipts.

Adds ``intake_threads.flow`` / ``contact_linkedin`` and per-message
``delivered_at`` / ``read_at`` so a LinkedIn scrape can drive a short text
intake and iMessage read receipts can surface in the CRM.

Revision ID: a9c4e21b7f50
Revises: e5f0a92c7d18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c4e21b7f50"
down_revision: str | None = "e5f0a92c7d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_threads",
        sa.Column("flow", sa.String(length=32), server_default="full", nullable=False),
    )
    op.add_column(
        "intake_threads",
        sa.Column(
            "contact_linkedin", sa.String(length=400), server_default="", nullable=False
        ),
    )
    op.add_column(
        "intake_messages",
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "intake_messages",
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_intake_messages_native_id",
        "intake_messages",
        ["provider_native_id"],
        sqlite_where=sa.text("provider_native_id != ''"),
        postgresql_where=sa.text("provider_native_id != ''"),
    )


def downgrade() -> None:
    op.drop_index("ix_intake_messages_native_id", table_name="intake_messages")
    op.drop_column("intake_messages", "read_at")
    op.drop_column("intake_messages", "delivered_at")
    op.drop_column("intake_threads", "contact_linkedin")
    op.drop_column("intake_threads", "flow")

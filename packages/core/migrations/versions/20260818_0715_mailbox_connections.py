"""microsoft graph mailbox connections

Revision ID: c8e1a0b4d2f9
Revises: 9f2e4c8b1d70
Create Date: 2026-08-18 07:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e1a0b4d2f9"
down_revision: str | None = "9f2e4c8b1d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mailbox_connections",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("mailbox_email", sa.String(length=255), nullable=False),
        sa.Column("mailbox_name", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", sa.String(length=80), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", sa.String(length=400), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "provider", name="uq_mailbox_org_provider"),
    )
    op.create_index(
        op.f("ix_mailbox_connections_org_id"), "mailbox_connections", ["org_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_mailbox_connections_org_id"), table_name="mailbox_connections")
    op.drop_table("mailbox_connections")

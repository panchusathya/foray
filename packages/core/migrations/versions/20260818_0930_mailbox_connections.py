"""mailbox connections (Outlook via Graph), sequence send-from, message graph ids

Revision ID: e6a1b4d92f08
Revises: b3d7f0a2c541
Create Date: 2026-08-18 09:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6a1b4d92f08"
down_revision: str | None = "b3d7f0a2c541"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mailbox_connections",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.String(length=40), nullable=True),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("ms_user_id", sa.String(length=80), nullable=False),
        sa.Column("token_blob", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("inbox_delta_link", sa.Text(), nullable=False),
        sa.Column("sent_delta_link", sa.Text(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "address", name="uq_mailbox_org_address"),
    )
    op.create_index(
        op.f("ix_mailbox_connections_org_id"), "mailbox_connections", ["org_id"], unique=False
    )

    # Batch mode: SQLite rebuilds the table to gain the FK; Postgres ALTERs.
    with op.batch_alter_table("sequences") as batch:
        batch.add_column(
            sa.Column(
                "mailbox_id",
                sa.String(length=40),
                sa.ForeignKey(
                    "mailbox_connections.id",
                    ondelete="SET NULL",
                    name="fk_sequences_mailbox_id",
                ),
                nullable=True,
            )
        )
    op.add_column(
        "messages",
        sa.Column("mailbox_id", sa.String(length=40), server_default="", nullable=False),
    )
    op.add_column(
        "messages",
        sa.Column("graph_message_id", sa.String(length=300), server_default="", nullable=False),
    )
    op.add_column(
        "messages",
        sa.Column(
            "internet_message_id", sa.String(length=300), server_default="", nullable=False
        ),
    )
    op.add_column(
        "messages",
        sa.Column("conversation_id", sa.String(length=200), server_default="", nullable=False),
    )
    op.create_index(
        op.f("ix_messages_internet_message_id"),
        "messages",
        ["internet_message_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_internet_message_id"), table_name="messages")
    op.drop_column("messages", "conversation_id")
    op.drop_column("messages", "internet_message_id")
    op.drop_column("messages", "graph_message_id")
    op.drop_column("messages", "mailbox_id")
    with op.batch_alter_table("sequences") as batch:
        batch.drop_column("mailbox_id")
    op.drop_index(op.f("ix_mailbox_connections_org_id"), table_name="mailbox_connections")
    op.drop_table("mailbox_connections")

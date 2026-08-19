"""Thread sync: messages can exist without an enrollment.

Manual Outlook mail (sent by hand, or a reply with no matching outbound) joins
the same messages table with enrollment_id NULL and source='outlook', so every
consumer — reply drafting, analytics, /v1/messages, the new timeline — reads
one table instead of a UNION. New columns carry what the sequencer never
needed: the counterparty address, the original HTML, Outlook's own deep link,
and an attachment count (never the bytes). thread_sync_enabled on the mailbox
gates all of it, off by default.

Revision ID: f3b7c1d84a25
Revises: a7c3e9f21b64 (W2's message_ai_fragments — merge the W2 PR first)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f3b7c1d84a25"
# Chained onto the W2 branch's migration so the two PRs keep one linear
# history. Merge order: claude/w2-ai-directives, then this branch.
down_revision: str | None = "a7c3e9f21b64"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot ALTER COLUMN and the FK is unnamed, so it needs a full
    # batch-recreate. Postgres must NOT recreate: reply_drafts FKs into
    # messages, so dropping messages_pkey mid-recreate fails with
    # DependentObjectsStillExist — plain ALTERs are both sufficient and safe.
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "never"
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.alter_column("enrollment_id", existing_type=sa.String(40), nullable=True)
        batch.add_column(sa.Column("source", sa.String(20), nullable=False, server_default="sequence"))
        batch.add_column(sa.Column("from_address", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("body_html", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("web_link", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("attachment_count", sa.Integer(), nullable=False, server_default="0")
        )

    # Existing inbound rows: the candidate's address went into to_address
    # (sequencer.record_reply copies it), and sent_at was never set — the
    # timeline sorts on it, so backfill from created_at.
    op.execute("UPDATE messages SET from_address = to_address WHERE direction = 'inbound'")
    op.execute(
        "UPDATE messages SET sent_at = created_at "
        "WHERE direction = 'inbound' AND sent_at IS NULL"
    )

    op.create_index("ix_messages_candidate_sent", "messages", ["candidate_id", "sent_at"])
    op.create_index("ix_messages_org_conversation", "messages", ["org_id", "conversation_id"])
    op.create_index(
        "uq_messages_org_graph_id",
        "messages",
        ["org_id", "graph_message_id"],
        unique=True,
        sqlite_where=sa.text("graph_message_id != ''"),
        postgresql_where=sa.text("graph_message_id != ''"),
    )

    # Match the models, which carry Python-side defaults only.
    with op.batch_alter_table("messages") as batch:
        batch.alter_column("source", server_default=None)
        batch.alter_column("from_address", server_default=None)
        batch.alter_column("body_html", server_default=None)
        batch.alter_column("web_link", server_default=None)
        batch.alter_column("attachment_count", server_default=None)

    op.add_column(
        "mailbox_connections",
        sa.Column("thread_sync_enabled", sa.Boolean(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("mailbox_connections") as batch:
        batch.alter_column("thread_sync_enabled", server_default=None)


def downgrade() -> None:
    op.drop_column("mailbox_connections", "thread_sync_enabled")
    op.drop_index("uq_messages_org_graph_id", table_name="messages")
    op.drop_index("ix_messages_org_conversation", table_name="messages")
    op.drop_index("ix_messages_candidate_sent", table_name="messages")
    op.execute("DELETE FROM messages WHERE enrollment_id IS NULL")
    recreate = "always" if op.get_bind().dialect.name == "sqlite" else "never"
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.drop_column("attachment_count")
        batch.drop_column("web_link")
        batch.drop_column("body_html")
        batch.drop_column("from_address")
        batch.drop_column("source")
        batch.alter_column("enrollment_id", existing_type=sa.String(40), nullable=False)

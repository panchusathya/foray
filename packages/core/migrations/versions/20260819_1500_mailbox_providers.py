"""Mail providers: Zoho alongside Outlook, and a pool of sending mailboxes.

Two changes that have to land together, because the second is unsafe without
the first.

The provider-neutral rename. ``messages.graph_message_id`` was never really a
Graph detail: it is the provider's own message id, and dedupe, the reply-draft
lookup and the partial unique index all key on it. A second provider writing
NULL there would silently break reply detection, double-store sent items, and
never generate an AI draft. It becomes ``provider_native_id``, the reply draft's
``graph_draft_id`` becomes ``provider_draft_id``, and ``ms_user_id`` becomes
``provider_account_id``. Synced mail also stops calling itself "outlook" and
names the provider it came from, so a Zoho thread is not labelled Outlook in
the timeline.

The sending pool. Mailboxes gain a rolling daily cap and an on/off switch;
sequences gain ``sender_mode``, and enrollments remember which mailbox first
emailed a candidate so every later step keeps the thread on one address. That
pin is ON DELETE RESTRICT on purpose: SET NULL would let rotation quietly move
a live conversation to a different address, and disconnecting revokes the row
rather than deleting it, so RESTRICT never fires in normal use.

sender_mode is backfilled to 'fixed' wherever a mailbox was already pinned.
Without that, every existing pinned sequence would start spraying across the
pool the moment this deploys. Rows with no mailbox stay 'rotate', which is the
intended change: they pick up the pool if one exists and fall through to the
globally configured provider if it does not.

Revision ID: c5d9a1f30e78
Revises: f3b7c1d84a25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c5d9a1f30e78"
down_revision: str | None = "f3b7c1d84a25"
branch_labels = None
depends_on = None


def _recreate() -> str:
    # SQLite cannot ALTER COLUMN, so it needs a full batch-recreate. Postgres
    # must NOT recreate: reply_drafts and enrollments FK into these tables, and
    # dropping a pkey mid-recreate fails with DependentObjectsStillExist.
    return "always" if op.get_bind().dialect.name == "sqlite" else "never"


def upgrade() -> None:
    recreate = _recreate()

    # ---- messages: provider-neutral native id ---------------------------
    # Drop the old partial index before touching the column it covers: SQLite's
    # batch recreate reflects indexes, and a partial index round-trips poorly.
    op.drop_index("uq_messages_org_graph_id", table_name="messages")
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("provider_native_id", sa.String(300), nullable=False, server_default="")
        )
    op.execute("UPDATE messages SET provider_native_id = graph_message_id")
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.drop_column("graph_message_id")
        batch.alter_column("provider_native_id", server_default=None)

    op.create_index(
        "uq_messages_org_native_id",
        "messages",
        ["org_id", "provider_native_id"],
        unique=True,
        sqlite_where=sa.text("provider_native_id != ''"),
        postgresql_where=sa.text("provider_native_id != ''"),
    )
    # Per-mailbox rolling volume, for the rotation caps.
    op.create_index("ix_messages_mailbox_sent", "messages", ["mailbox_id", "sent_at"])
    # Synced mail names its provider now instead of calling everything Outlook.
    op.execute("UPDATE messages SET source = 'microsoft' WHERE source = 'outlook'")

    # ---- reply_drafts ---------------------------------------------------
    with op.batch_alter_table("reply_drafts", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("provider_draft_id", sa.String(300), nullable=False, server_default="")
        )
    op.execute("UPDATE reply_drafts SET provider_draft_id = graph_draft_id")
    with op.batch_alter_table("reply_drafts", recreate=recreate) as batch:
        batch.drop_column("graph_draft_id")
        batch.alter_column("provider_draft_id", server_default=None)

    # ---- mailbox_connections: identity, endpoints, rotation -------------
    with op.batch_alter_table("mailbox_connections", recreate=recreate) as batch:
        batch.add_column(
            sa.Column(
                "provider_account_id", sa.String(120), nullable=False, server_default=""
            )
        )
        batch.add_column(sa.Column("api_base", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("auth_base", sa.String(255), nullable=False, server_default=""))
        batch.add_column(sa.Column("sync_state", sa.JSON(), nullable=False, server_default="{}"))
        batch.add_column(
            sa.Column("send_enabled", sa.Boolean(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("daily_send_cap", sa.Integer(), nullable=False, server_default="40")
        )
    op.execute("UPDATE mailbox_connections SET provider_account_id = ms_user_id")
    with op.batch_alter_table("mailbox_connections", recreate=recreate) as batch:
        batch.drop_column("ms_user_id")
        batch.alter_column("provider_account_id", server_default=None)
        batch.alter_column("api_base", server_default=None)
        batch.alter_column("auth_base", server_default=None)
        batch.alter_column("sync_state", server_default=None)
        batch.alter_column("send_enabled", server_default=None)
        batch.alter_column("daily_send_cap", server_default=None)

    # ---- sequences: how the sender is chosen ----------------------------
    with op.batch_alter_table("sequences", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("sender_mode", sa.String(16), nullable=False, server_default="rotate")
        )
    # Anything already pinned to a mailbox stays pinned. This line is the whole
    # reason the default is safe to deploy.
    op.execute("UPDATE sequences SET sender_mode = 'fixed' WHERE mailbox_id IS NOT NULL")
    with op.batch_alter_table("sequences", recreate=recreate) as batch:
        batch.alter_column("sender_mode", server_default=None)

    # ---- enrollments: the pinned mailbox --------------------------------
    with op.batch_alter_table("enrollments", recreate=recreate) as batch:
        batch.add_column(sa.Column("mailbox_id", sa.String(40), nullable=True))
        batch.create_foreign_key(
            "fk_enrollments_mailbox_id",
            "mailbox_connections",
            ["mailbox_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    # Existing enrollments inherit their sequence's pin, so a sequence mid-flight
    # keeps threading from the same address instead of being handed to the pool
    # on its next step.
    op.execute(
        "UPDATE enrollments SET mailbox_id = ("
        " SELECT sequences.mailbox_id FROM sequences"
        " WHERE sequences.id = enrollments.sequence_id"
        ") WHERE mailbox_id IS NULL"
    )


def downgrade() -> None:
    recreate = _recreate()

    with op.batch_alter_table("enrollments", recreate=recreate) as batch:
        batch.drop_constraint("fk_enrollments_mailbox_id", type_="foreignkey")
        batch.drop_column("mailbox_id")

    with op.batch_alter_table("sequences", recreate=recreate) as batch:
        batch.drop_column("sender_mode")

    with op.batch_alter_table("mailbox_connections", recreate=recreate) as batch:
        batch.add_column(sa.Column("ms_user_id", sa.String(80), nullable=False, server_default=""))
    op.execute("UPDATE mailbox_connections SET ms_user_id = provider_account_id")
    with op.batch_alter_table("mailbox_connections", recreate=recreate) as batch:
        batch.alter_column("ms_user_id", server_default=None)
        batch.drop_column("daily_send_cap")
        batch.drop_column("send_enabled")
        batch.drop_column("sync_state")
        batch.drop_column("auth_base")
        batch.drop_column("api_base")
        batch.drop_column("provider_account_id")

    with op.batch_alter_table("reply_drafts", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("graph_draft_id", sa.String(300), nullable=False, server_default="")
        )
    op.execute("UPDATE reply_drafts SET graph_draft_id = provider_draft_id")
    with op.batch_alter_table("reply_drafts", recreate=recreate) as batch:
        batch.alter_column("graph_draft_id", server_default=None)
        batch.drop_column("provider_draft_id")

    op.execute("UPDATE messages SET source = 'outlook' WHERE source IN ('microsoft', 'zoho')")
    op.drop_index("ix_messages_mailbox_sent", table_name="messages")
    op.drop_index("uq_messages_org_native_id", table_name="messages")
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.add_column(
            sa.Column("graph_message_id", sa.String(300), nullable=False, server_default="")
        )
    op.execute("UPDATE messages SET graph_message_id = provider_native_id")
    with op.batch_alter_table("messages", recreate=recreate) as batch:
        batch.alter_column("graph_message_id", server_default=None)
        batch.drop_column("provider_native_id")
    op.create_index(
        "uq_messages_org_graph_id",
        "messages",
        ["org_id", "graph_message_id"],
        unique=True,
        sqlite_where=sa.text("graph_message_id != ''"),
        postgresql_where=sa.text("graph_message_id != ''"),
    )

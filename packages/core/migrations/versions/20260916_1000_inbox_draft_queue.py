"""inbox draft queue

Drafted replies to ordinary inbox mail, held for review instead of being
written straight into the mailbox.

Its own table rather than a nullable ``messages.candidate_id``: that column is
NOT NULL and indexed as the spine of the CRM timeline, and widening it so a
stranger's email could be stored would change every row in the system to serve
a queue that is read on one screen.

Revision ID: f3a7c1d94e28
Revises: c4f8e2a91b70
Create Date: 2026-09-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a7c1d94e28"
down_revision = "c4f8e2a91b70"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox_drafts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("mailbox_id", sa.String(length=40), nullable=False),
        sa.Column("inbound_native_id", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("inbound_thread_id", sa.String(length=300), nullable=False, server_default=""),
        sa.Column(
            "inbound_rfc_message_id", sa.String(length=300), nullable=False, server_default=""
        ),
        sa.Column("from_address", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("from_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("subject", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("inbound_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("web_link", sa.Text(), nullable=False, server_default=""),
        sa.Column("candidate_id", sa.String(length=40), nullable=True),
        sa.Column("message_id", sa.String(length=40), nullable=True),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("generated_body", sa.Text(), nullable=False, server_default=""),
        sa.Column("generated_by", sa.String(length=20), nullable=False, server_default="template"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("provider_draft_id", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("draft_web_link", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.String(length=400), nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mailbox_id"], ["mailbox_connections.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: deleting a contact must not silently drop a
        # reply the user was about to send.
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # The sync loop re-reads a folder on every poll, so without this a
        # message that sits unanswered gets a fresh draft on every run.
        sa.UniqueConstraint("org_id", "inbound_native_id", name="uq_inbox_draft_inbound"),
    )
    op.create_index("ix_inbox_drafts_org_id", "inbox_drafts", ["org_id"])
    op.create_index("ix_inbox_drafts_mailbox_id", "inbox_drafts", ["mailbox_id"])
    op.create_index("ix_inbox_drafts_candidate_id", "inbox_drafts", ["candidate_id"])
    op.create_index("ix_inbox_drafts_received_at", "inbox_drafts", ["received_at"])
    op.create_index("ix_inbox_drafts_status", "inbox_drafts", ["status"])
    op.create_index(
        "ix_inbox_drafts_org_status", "inbox_drafts", ["org_id", "status", "received_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_drafts_org_status", table_name="inbox_drafts")
    op.drop_index("ix_inbox_drafts_status", table_name="inbox_drafts")
    op.drop_index("ix_inbox_drafts_received_at", table_name="inbox_drafts")
    op.drop_index("ix_inbox_drafts_candidate_id", table_name="inbox_drafts")
    op.drop_index("ix_inbox_drafts_mailbox_id", table_name="inbox_drafts")
    op.drop_index("ix_inbox_drafts_org_id", table_name="inbox_drafts")
    op.drop_table("inbox_drafts")

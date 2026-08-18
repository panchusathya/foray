"""reply drafts: AI-written replies placed in the recruiter's Outlook Drafts

Revision ID: d94c2e7f5a13
Revises: e6a1b4d92f08
Create Date: 2026-08-18 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d94c2e7f5a13"
down_revision: str | None = "e6a1b4d92f08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reply_drafts",
        sa.Column("id", sa.String(length=40), nullable=False),
        sa.Column("org_id", sa.String(length=40), nullable=False),
        sa.Column("mailbox_id", sa.String(length=40), nullable=False),
        sa.Column("inbound_message_id", sa.String(length=40), nullable=False),
        sa.Column("candidate_id", sa.String(length=40), nullable=True),
        sa.Column("graph_draft_id", sa.String(length=300), nullable=False),
        sa.Column("web_link", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("generated_by", sa.String(length=20), nullable=False),
        sa.Column("intent", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("final_sent_text", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["mailbox_id"], ["mailbox_connections.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["inbound_message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("inbound_message_id", name="uq_reply_draft_inbound"),
    )
    op.create_index(op.f("ix_reply_drafts_org_id"), "reply_drafts", ["org_id"], unique=False)
    op.create_index(
        op.f("ix_reply_drafts_mailbox_id"), "reply_drafts", ["mailbox_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_reply_drafts_mailbox_id"), table_name="reply_drafts")
    op.drop_index(op.f("ix_reply_drafts_org_id"), table_name="reply_drafts")
    op.drop_table("reply_drafts")

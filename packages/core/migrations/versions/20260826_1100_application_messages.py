"""Keep the mail an employer sends to a candidate's application alias.

Owning the alias made us the addressee of record, and until now that mail was
classified, acted on, relayed to the candidate's own inbox and then gone from
our side. Only an event survived, carrying the sender and a subject truncated
to 200 characters -- enough to move a status, not enough to show anybody what
was actually said. The portal tracker reads these rows.

``application_id`` is nullable because attribution is two different questions.
Which *candidate* a message belongs to is certain: the alias local part is an
HMAC of their id. Which of their open *applications* it answers is a guess from
the sender's domain, and an unmatched message still belongs in their inbox.

The unique index on (org_id, provider_native_id) is what makes a replayed sync
page free. Without it a replay clicks a verification link twice and re-moves a
status a human may already have corrected.

Revision ID: d5b2f8a41c93
Revises: a91d3f70c5b8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5b2f8a41c93"
down_revision: str | None = "a91d3f70c5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "application_messages",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SET NULL, not CASCADE: a withdrawn application should not delete the
        # employer's side of the conversation.
        sa.Column(
            "application_id",
            sa.String(length=40),
            sa.ForeignKey("applications.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="other"),
        sa.Column("alias_address", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("from_address", sa.String(length=320), nullable=False, server_default=""),
        sa.Column("from_name", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("subject", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("provider_native_id", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "relayed", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Partial: SQL compares '' to '' as equal, so a plain unique constraint
    # would let the first row with no native id block every later one.
    op.create_index(
        "uq_application_message_native",
        "application_messages",
        ["org_id", "provider_native_id"],
        unique=True,
        postgresql_where=sa.text("provider_native_id != ''"),
        sqlite_where=sa.text("provider_native_id != ''"),
    )
    op.create_index("ix_application_messages_org_id", "application_messages", ["org_id"])
    op.create_index(
        "ix_application_messages_candidate",
        "application_messages",
        ["candidate_id", "received_at"],
    )
    op.create_index(
        "ix_application_messages_application", "application_messages", ["application_id"]
    )
    op.create_index(
        "ix_application_messages_org_kind", "application_messages", ["org_id", "kind"]
    )


def downgrade() -> None:
    op.drop_index("uq_application_message_native", table_name="application_messages")
    op.drop_index("ix_application_messages_org_kind", table_name="application_messages")
    op.drop_index("ix_application_messages_application", table_name="application_messages")
    op.drop_index("ix_application_messages_candidate", table_name="application_messages")
    op.drop_index("ix_application_messages_org_id", table_name="application_messages")
    op.drop_table("application_messages")

"""Per-application aliases, and the columns a reply needs.

Two changes, one purpose: make a reply possible and make it attributable.

``candidate_aliases`` gains ``application_id`` and ``scope``. An alias minted
from now on is per application, so an employer's reply identifies the pursuit
exactly instead of leaving it to ``inbox._match_application``, which matches the
sender's domain against the posting and correctly declines when two
applications sit behind one ATS vendor's domain. Existing rows are
candidate-scoped and employers are already writing to them, so ``scope``
defaults to ``candidate`` and the row -- not the address -- says which HMAC
verifies it.

``application_messages`` gains ``direction``, ``reply_to_address`` and
``rfc_message_id``. Outbound replies live in the same table so a thread reads
as a conversation; ``reply_to_address`` is where a reply actually goes, since
ATS mail is sent from a robot and carries the routing address in Reply-To; and
``rfc_message_id`` is what the reply threads off at the employer's end.

Revision ID: f7c31a9e2d48
Revises: d5b2f8a41c93
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7c31a9e2d48"
down_revision: str | None = "d5b2f8a41c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode: SQLite rebuilds the table to gain the FK; Postgres ALTERs.
    # `op.create_foreign_key` alone raises NotImplementedError on SQLite.
    with op.batch_alter_table("candidate_aliases") as batch:
        batch.add_column(
            sa.Column(
                "application_id",
                sa.String(length=40),
                sa.ForeignKey(
                    "applications.id",
                    ondelete="SET NULL",
                    name="fk_candidate_aliases_application",
                ),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column("scope", sa.String(length=16), nullable=False, server_default="candidate")
        )

    op.add_column(
        "application_messages",
        sa.Column("direction", sa.String(length=16), nullable=False, server_default="inbound"),
    )
    op.add_column(
        "application_messages",
        sa.Column("reply_to_address", sa.String(length=320), nullable=False, server_default=""),
    )
    op.add_column(
        "application_messages",
        sa.Column("rfc_message_id", sa.String(length=400), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("application_messages", "rfc_message_id")
    op.drop_column("application_messages", "reply_to_address")
    op.drop_column("application_messages", "direction")
    with op.batch_alter_table("candidate_aliases") as batch:
        batch.drop_column("scope")
        batch.drop_column("application_id")

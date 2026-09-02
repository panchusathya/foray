"""Let a rotating sequence rotate over a chosen subset of mailboxes.

``sender_mode`` was all-or-one: rotate across every eligible mailbox in the
org, or pin to exactly one. Neither says "use these three", which is what a
recruiter wants when one address carries a different signature, a warming
mailbox should not take production volume, or a client-specific address must
not appear on unrelated outreach.

Empty is the important default. It means "every eligible mailbox", so every
existing sequence keeps rotating exactly as it does today and mailboxes
connected later join those rotations without anyone editing a sequence. Only
a non-empty list narrows the pool.

Revision ID: a1c6e3f92d70
Revises: f3a9d7c2b481
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c6e3f92d70"
down_revision: str | Sequence[str] | None = "f3a9d7c2b481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sequences",
        sa.Column("mailbox_ids", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("sequences", "mailbox_ids")

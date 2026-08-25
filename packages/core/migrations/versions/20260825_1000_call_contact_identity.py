"""Voice intake learns who called: name and email off the call itself.

The inbound agent has always asked for both. Nothing stored the answer, so a
call from a number that was not already in ``candidate_phones`` produced a
record no person was attached to -- and the review queue could not tell an
evasive caller from one who had introduced themselves properly.

Empty string, not NULL, matching every other text column on the row: a reader
should never have to distinguish "asked and refused" from "column added later".

Revision ID: c4a17e2b8d63
Revises: b3f7a06d94c2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a17e2b8d63"
down_revision: str | None = "b3f7a06d94c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_calls",
        sa.Column("contact_name", sa.String(length=240), server_default="", nullable=False),
    )
    op.add_column(
        "intake_calls",
        sa.Column("contact_email", sa.String(length=255), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("intake_calls", "contact_email")
    op.drop_column("intake_calls", "contact_name")

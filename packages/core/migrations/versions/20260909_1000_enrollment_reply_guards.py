"""Reply-side state on enrollments.

A candidate who wrote "In Japan until 9/10 please f/u after" was classified
as an auto-responder, the only intent that keeps a sequence alive, and the
next step went out on schedule. Another replied "interested" to one sequence
and was mailed twice more by a second one. Both need the enrollment to carry
what the reply said: when a human answered (``replied_at``), the date they
asked to be contacted after (``follow_up_after``), whether the reminder to the
recruiter went out (``follow_up_reminder_sent_at``), and how far a genuine
out-of-office pushed the next send (``snoozed_until``).

All nullable, no backfill: existing rows read as "no reply recorded", which
is what they were.

Revision ID: b1e6d4a9c2f7
Revises: f3a9d7c2b481
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1e6d4a9c2f7"
down_revision: str | Sequence[str] | None = "f3a9d7c2b481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "enrollments",
        sa.Column("replied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("enrollments", sa.Column("follow_up_after", sa.Date(), nullable=True))
    op.add_column(
        "enrollments",
        sa.Column("follow_up_reminder_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "enrollments",
        sa.Column("snoozed_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("enrollments", "snoozed_until")
    op.drop_column("enrollments", "follow_up_reminder_sent_at")
    op.drop_column("enrollments", "follow_up_after")
    op.drop_column("enrollments", "replied_at")

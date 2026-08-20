"""Remember which suggested duplicates are actually two different people.

Duplicate suggestions are recomputed from live data on every request rather
than stored, which keeps them from going stale -- but it also means a pair
somebody has already judged comes back forever. Two colleagues who share a
name would be re-suggested every time the list is opened, and a list that
keeps asking the same answered question stops being read.

The pair is stored with the ids sorted so it matches whichever way round it is
later suggested.

Revision ID: f5a83c0d21e7
Revises: e2b6d4c17f90
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f5a83c0d21e7"
down_revision: str | None = "e2b6d4c17f90"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "duplicate_dismissals",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "left_id",
            sa.String(40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "right_id",
            sa.String(40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dismissed_by", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("left_id", "right_id", name="uq_duplicate_dismissal"),
    )
    op.create_index("ix_duplicate_dismissals_org_id", "duplicate_dismissals", ["org_id"])
    op.create_index("ix_duplicate_dismissals_left_id", "duplicate_dismissals", ["left_id"])
    op.create_index("ix_duplicate_dismissals_right_id", "duplicate_dismissals", ["right_id"])


def downgrade() -> None:
    op.drop_index("ix_duplicate_dismissals_right_id", table_name="duplicate_dismissals")
    op.drop_index("ix_duplicate_dismissals_left_id", table_name="duplicate_dismissals")
    op.drop_index("ix_duplicate_dismissals_org_id", table_name="duplicate_dismissals")
    op.drop_table("duplicate_dismissals")

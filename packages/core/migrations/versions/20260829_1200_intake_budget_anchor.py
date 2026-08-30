"""Anchor a stated time budget to when it was stated.

``gate_keys_for`` sizes the remaining questions against the clock, and the
clock it was given ran from ``started_at`` -- the beginning of the call. So a
caller who said "just a minute" fifty seconds in was credited with ten
seconds, which buys zero exchanges, which trims the gate to its single most
valuable question. That question was already answered, so the gate closed and
the agent wrapped up without asking anything at all.

The budget is a promise about the future, not a total that includes the
greeting. This column records the moment it was made so the arithmetic can
measure from there.

Nullable, and null is meaningful in two ways that want identical handling: a
call that never stated a budget, and a call that predates this column. Both
fall back to a zero clock, which is the pre-clock behaviour.

Revision ID: f3a9d7c2b481
Revises: f4a8c2d9e761
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9d7c2b481"
down_revision: str | Sequence[str] | None = "f4a8c2d9e761"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_calls",
        sa.Column("budget_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("intake_calls", "budget_started_at")

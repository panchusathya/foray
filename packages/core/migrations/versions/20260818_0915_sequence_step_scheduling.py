"""sequence step scheduling fields (delay unit, send-at time, timezone)

Revision ID: c1a7e9d3b512
Revises: 9f2e4c8b1d70
Create Date: 2026-08-18 09:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a7e9d3b512"
down_revision: str | None = "9f2e4c8b1d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sequence_steps",
        sa.Column("delay_unit", sa.String(length=16), server_default="days", nullable=False),
    )
    op.add_column(
        "sequence_steps",
        sa.Column("send_at_time", sa.String(length=5), server_default="", nullable=False),
    )
    op.add_column(
        "sequence_steps",
        sa.Column("send_timezone", sa.String(length=64), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("sequence_steps", "send_timezone")
    op.drop_column("sequence_steps", "send_at_time")
    op.drop_column("sequence_steps", "delay_unit")

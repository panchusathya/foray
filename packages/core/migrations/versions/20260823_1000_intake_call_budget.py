"""Stated time budget on intake_calls.

A candidate who says they have three minutes gets a narrower gate (core
fields only). The budget is data, not a prompt heuristic: minutes_available
is persisted on first sight of the check_coverage argument, gate_mode records
which set closed the call, and deferred_fields lists required keys we chose
not to ask so an empty field is not read as forgotten.

None / unset reproduces the full required set. A stated budget may only
narrow the gate.

Revision ID: e7c4a19b2d80
Revises: d2b8c31f9e47
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7c4a19b2d80"
down_revision: str | None = "d2b8c31f9e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("intake_calls") as batch:
        batch.add_column(sa.Column("minutes_available", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column("gate_mode", sa.String(length=8), server_default="full", nullable=False)
        )
        batch.add_column(sa.Column("deferred_fields", sa.JSON(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("intake_calls") as batch:
        batch.drop_column("deferred_fields")
        batch.drop_column("gate_mode")
        batch.drop_column("minutes_available")

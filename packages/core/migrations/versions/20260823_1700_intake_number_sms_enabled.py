"""Add sms_enabled to intake_numbers."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3a8d51b62c0"
down_revision: str | None = "b7e2c94af013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("intake_numbers") as batch:
        batch.add_column(
            sa.Column("sms_enabled", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_numbers") as batch:
        batch.drop_column("sms_enabled")

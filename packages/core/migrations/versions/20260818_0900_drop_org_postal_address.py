"""drop org postal address (sends come from the user's own mailbox)

Revision ID: b3d7f0a2c541
Revises: 9f2e4c8b1d70
Create Date: 2026-08-18 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3d7f0a2c541"
down_revision: str | None = "9f2e4c8b1d70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("orgs", "postal_address")


def downgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("postal_address", sa.String(length=400), server_default="", nullable=False),
    )

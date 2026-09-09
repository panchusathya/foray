"""Org-level pause for candidate-facing sequence email.

A timestamp rather than a boolean so the Settings banner can say how long
sending has been off. Null on both columns is the live state. Per-org
because prod has more than one tenant sharing the database.

Revision ID: c4f8e2a91b70
Revises: d7b2e9c4a1f6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f8e2a91b70"
down_revision: str | Sequence[str] | None = "d7b2e9c4a1f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("outreach_paused_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "orgs",
        sa.Column("outreach_paused_by", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orgs", "outreach_paused_by")
    op.drop_column("orgs", "outreach_paused_at")

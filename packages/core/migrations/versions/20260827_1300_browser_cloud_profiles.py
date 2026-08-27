"""Per-candidate per-origin Browser Use Cloud profiles.

Revision ID: e8b3c1d4a7f2
Revises: d7e4b2a1c9f6
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8b3c1d4a7f2"
down_revision: str | None = "d7e4b2a1c9f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "browser_cloud_profiles",
        sa.Column("id", sa.String(length=40), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(length=40),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "candidate_id",
            sa.String(length=40),
            sa.ForeignKey("candidates.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("origin", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("user_id", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("cloud_profile_id", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "org_id", "candidate_id", "origin", name="uq_browser_cloud_profile_origin"
        ),
    )


def downgrade() -> None:
    op.drop_table("browser_cloud_profiles")

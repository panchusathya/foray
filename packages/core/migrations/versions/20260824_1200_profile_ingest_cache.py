"""Negative cache for the paid LinkedIn profile fetch.

The enrichment rule in packages/foray/AGENTS.md, applied to the new profile
provider: a real miss stamps ``profile_ingest_checked_at`` so the same person
is never paid for twice, and a provider failure leaves it unstamped so a
vendor's quota wall cannot permanently mark somebody unfetchable. ``profile_ingest_error``
records why the last attempt yielded nothing.

Revision ID: f2b6d94c81a7
Revises: c7d2f83a91e4
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b6d94c81a7"
down_revision: str | None = "c7d2f83a91e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candidates",
        sa.Column("profile_ingest_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "candidates",
        sa.Column(
            "profile_ingest_error", sa.String(length=400), server_default="", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("candidates", "profile_ingest_error")
    op.drop_column("candidates", "profile_ingest_checked_at")

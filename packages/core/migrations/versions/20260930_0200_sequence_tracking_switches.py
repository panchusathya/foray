"""sequence tracking switches

Per-sequence Track opens / Track clicks. Both default on, so every existing
sequence keeps sending exactly what it sent before; switching one off drops
the hidden pixel or the /t/c link rewrite from that sequence's emails.

Revision ID: e8b2c5d1f4a6
Revises: d4f1a7c3e290
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e8b2c5d1f4a6"
down_revision = "d4f1a7c3e290"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sequences",
        sa.Column("track_opens", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "sequences",
        sa.Column("track_clicks", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("sequences", "track_clicks")
    op.drop_column("sequences", "track_opens")

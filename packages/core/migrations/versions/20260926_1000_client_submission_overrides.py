"""client submission overrides

Which profile fields a recruiter edited on a client dashboard. Everything else
is now read live from the CRM contact, so a title fixed in the CRM reaches the
client; an edit made on the dashboard itself is kept instead.

Revision ID: c7e5a2b8d913
Revises: 9b2d6e4a1c57
Create Date: 2026-09-26
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7e5a2b8d913"
down_revision = "9b2d6e4a1c57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "client_submissions",
        sa.Column("overridden_fields", sa.JSON(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("client_submissions", "overridden_fields")

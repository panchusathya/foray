"""org send priority

Which steps go first when the send line is backed up. Defaults to first
emails ahead of follow-ups; editable from the Outbox.

Revision ID: a7c4e1f9b2d3
Revises: f3a9d2b7c1e4
Create Date: 2026-10-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7c4e1f9b2d3"
down_revision = "f3a9d2b7c1e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column(
            "send_priority", sa.String(32), nullable=False, server_default="first_emails"
        ),
    )


def downgrade() -> None:
    op.drop_column("orgs", "send_priority")

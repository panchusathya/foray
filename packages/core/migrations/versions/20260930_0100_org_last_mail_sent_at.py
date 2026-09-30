"""org last_mail_sent_at

When an org's mailboxes last sent anything automated. Every mailbox send
stamps it, and the next one waits the configured gap after it, so no two
emails leave back to back whichever path sent them.

Revision ID: d4f1a7c3e290
Revises: c7e5a2b8d913
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4f1a7c3e290"
down_revision = "c7e5a2b8d913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orgs",
        sa.Column("last_mail_sent_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("orgs", "last_mail_sent_at")

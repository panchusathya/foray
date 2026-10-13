"""seed auto-throttle

Per-mailbox throttle state decided from seed results (deliverability/
throttle.py) and the org's mode for it. Every existing mailbox starts "ok" and
every org starts in "watch", so deploying this changes no sending.

Revision ID: a7e2c9f4d6b3
Revises: f4c8d2e6b1a9
Create Date: 2026-10-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a7e2c9f4d6b3"
down_revision = "f4c8d2e6b1a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mailbox_connections") as batch:
        batch.add_column(
            sa.Column("throttle_state", sa.String(16), nullable=False, server_default="ok")
        )
        batch.add_column(sa.Column("throttle_reason", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column("throttle_changed_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("rest_until", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("orgs") as batch:
        batch.add_column(
            sa.Column("auto_throttle", sa.String(8), nullable=False, server_default="watch")
        )


def downgrade() -> None:
    with op.batch_alter_table("orgs") as batch:
        batch.drop_column("auto_throttle")
    with op.batch_alter_table("mailbox_connections") as batch:
        batch.drop_column("rest_until")
        batch.drop_column("throttle_changed_at")
        batch.drop_column("throttle_reason")
        batch.drop_column("throttle_state")

"""mailbox warm-up

A newly connected mailbox ramps up to its daily cap instead of starting there
(deliverability/warmup.py). Existing mailboxes stay NULL -- not warming -- so
the twenty that are already sending are untouched.

Revision ID: f4c8d2e6b1a9
Revises: e3b7c1d9a4f2
Create Date: 2026-10-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f4c8d2e6b1a9"
down_revision = "e3b7c1d9a4f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("mailbox_connections") as batch:
        batch.add_column(sa.Column("warmup_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("mailbox_connections") as batch:
        batch.drop_column("warmup_started_at")

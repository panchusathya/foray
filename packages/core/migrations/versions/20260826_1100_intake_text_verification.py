"""Tie a live-call text verification to its exact Linq thread.

The first Linq message is sent while the candidate is still on the voice call,
but job details must wait for a real inbound reply.  These columns make that
state durable and let the inbound webhook prove which voice call was confirmed.

Revision ID: d4f8a2c6e901
Revises: b7c2e94a1d05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c6e901"
down_revision: str | None = "b7c2e94a1d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("intake_calls") as batch:
        batch.add_column(sa.Column("text_verification_thread_id", sa.String(length=40)))
        batch.add_column(
            sa.Column("text_verification_status", sa.String(length=16), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("text_verification_sent_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("text_verification_confirmed_at", sa.DateTime(timezone=True)))
        batch.create_foreign_key(
            "fk_intake_calls_text_verification_thread_id",
            "intake_threads",
            ["text_verification_thread_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index(
            "ix_intake_calls_text_verification_thread_id", ["text_verification_thread_id"]
        )
        batch.alter_column("text_verification_status", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("intake_calls") as batch:
        batch.drop_index("ix_intake_calls_text_verification_thread_id")
        batch.drop_constraint("fk_intake_calls_text_verification_thread_id", type_="foreignkey")
        batch.drop_column("text_verification_confirmed_at")
        batch.drop_column("text_verification_sent_at")
        batch.drop_column("text_verification_status")
        batch.drop_column("text_verification_thread_id")

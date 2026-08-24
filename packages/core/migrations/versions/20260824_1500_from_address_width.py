"""Widen intake_threads.from_address for Apple ID senders.

An iMessage sender is not always a phone number: Apple ID email addresses
arrive in the same field, and 64 characters truncates a long one. A truncated
sender is a thread nobody can find again — including the ``(channel,
from_address)`` STOP lookup, which is the one that must never fail.

``to_address`` stays 64: that side is always a number we own.

Revision ID: b3f7a06d94c2
Revises: d9e4b72c58a1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f7a06d94c2"
down_revision: str | None = "d9e4b72c58a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table so SQLite gets its table rebuild and Postgres gets a
    # plain ALTER; widening never truncates, so no data check is needed.
    with op.batch_alter_table("intake_threads") as batch:
        batch.alter_column(
            "from_address",
            existing_type=sa.String(length=64),
            type_=sa.String(length=255),
            existing_nullable=False,
            existing_server_default="",
        )


def downgrade() -> None:
    with op.batch_alter_table("intake_threads") as batch:
        batch.alter_column(
            "from_address",
            existing_type=sa.String(length=255),
            type_=sa.String(length=64),
            existing_nullable=False,
            existing_server_default="",
        )

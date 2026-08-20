"""Somewhere to keep the history import's progress.

The import re-enqueues itself per batch of addresses, so any counter kept in
the job payload dies with the job that held it. Without somewhere durable, the
UI has nothing to show and the user is blind: the button says "queued" and then
either mail appears or it does not, with no way to tell which is happening or
whether there was anything to import in the first place.

Same shape and same reasoning as ``discovery_state``, added alongside it.

Revision ID: e2b6d4c17f90
Revises: c7f2a05e83b1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "e2b6d4c17f90"
down_revision: str | None = "c7f2a05e83b1"
branch_labels = None
depends_on = None


def _recreate() -> str:
    # SQLite cannot ALTER COLUMN; Postgres must not recreate this table because
    # enrollments FKs into it with ON DELETE RESTRICT.
    return "always" if op.get_bind().dialect.name == "sqlite" else "never"


def upgrade() -> None:
    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.add_column(
            sa.Column("backfill_state", sa.JSON(), nullable=False, server_default="{}")
        )
    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.alter_column("backfill_state", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("mailbox_connections", recreate=_recreate()) as batch:
        batch.drop_column("backfill_state")

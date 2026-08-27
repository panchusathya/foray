"""What the wrap-up pitch actually said, kept on the call row.

``intake_calls.matches_offered`` holds the teaser lines spoken at the end of a
voice intake — [{posting_id, line, score}] — exactly as composed by
``intake/teasers.py``. On the call row rather than recomputed, for the same
reason the gate snapshot is: the jobs book moves and the record gets edited,
and this is evidence about what this caller was actually told.

Revision ID: b7c2e94a1d05
Revises: a91d3f70c5b8
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c2e94a1d05"
down_revision: str | None = "a91d3f70c5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_calls",
        sa.Column("matches_offered", sa.JSON(), nullable=False, server_default="[]"),
    )
    # Backfill done; the model has no server default, and `alembic check`
    # compares the two. Same two-step as messages.ai_fragments.
    with op.batch_alter_table("intake_calls") as batch:
        batch.alter_column("matches_offered", server_default=None)


def downgrade() -> None:
    op.drop_column("intake_calls", "matches_offered")

"""messages.ai_fragments: resolved {{ai: ...}} text, keyed per directive.

Stored on the message because (enrollment, step) is the send's idempotency
key — a retried send must reuse the exact fragments that may already have
reached the candidate, and the recruiter can audit what the model wrote.

Revision ID: a7c3e9f21b64
Revises: d94c2e7f5a13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e9f21b64"
down_revision: str | None = "d94c2e7f5a13"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("ai_fragments", sa.JSON(), nullable=False, server_default="{}"),
    )
    with op.batch_alter_table("messages") as batch:
        batch.alter_column("ai_fragments", server_default=None)


def downgrade() -> None:
    op.drop_column("messages", "ai_fragments")

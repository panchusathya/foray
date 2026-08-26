"""Inbound voice memos, and the protocol a thread is actually on.

``body_source`` says whether the words were typed or heard. It is not
bookkeeping: the heuristic extractor is regexes with no notion of speech, and
an ASR transcript arrives with almost no punctuation.

``intake_threads.last_service`` records what Linq delivered over -- iMessage,
RCS or SMS. Empty means never reported, which is deliberately distinct from
SMS.

Revision ID: b6e2d41a90f7
Revises: d5b2f8a41c93
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b6e2d41a90f7"
down_revision: str | None = "d5b2f8a41c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_messages",
        sa.Column("body_source", sa.String(length=16), server_default="", nullable=False),
    )
    op.add_column(
        "intake_threads",
        sa.Column("last_service", sa.String(length=16), server_default="", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("intake_threads", "last_service")
    op.drop_column("intake_messages", "body_source")

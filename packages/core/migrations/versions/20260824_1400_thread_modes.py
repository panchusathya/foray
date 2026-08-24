"""Thread modes: the coverage-gate interview grows into a recruiting manager.

``mode`` defaults to "intake" so every existing thread (and the whole existing
test suite) keeps its exact behaviour; the candidate-agent modes
(onboarding -> profile -> managing) are opt-in per thread at open time.
``last_presented`` is the reference frame for "apply to 2" and "the Goldman
one" -- the cards most recently shown, frozen as JSON.

Revision ID: d9e4b72c58a1
Revises: 8a5c31e7d2f9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d9e4b72c58a1"
down_revision: str | None = "8a5c31e7d2f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "intake_threads",
        sa.Column("mode", sa.String(length=24), server_default="intake", nullable=False),
    )
    op.add_column(
        "intake_threads",
        sa.Column("linkedin_url", sa.String(length=400), server_default="", nullable=False),
    )
    # JSON with no server default: SQLite and Postgres disagree on JSON
    # defaults, and the ORM default covers every writer. Existing rows read
    # back as NULL, which every consumer already treats as "nothing shown yet"
    # via `thread.last_presented or []`.
    op.add_column("intake_threads", sa.Column("last_presented", sa.JSON(), nullable=True))
    op.add_column(
        "intake_threads", sa.Column("presented_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("intake_threads", "presented_at")
    op.drop_column("intake_threads", "last_presented")
    op.drop_column("intake_threads", "linkedin_url")
    op.drop_column("intake_threads", "mode")

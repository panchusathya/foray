"""Rejoin the apply-by-link line with the vault/pitch merge.

Fourth round of the same story: apply-by-link (posting_form_questions,
e2c91b7a5d04) parented onto b3f7a06d94c2 before the vault and pitch merges
landed, so merging its branch into main leaves two heads again. The
``form_questions`` column it adds is disjoint from both other tracks, so this
adds no schema and exists only to give ``alembic upgrade head`` one head.

Empty ``upgrade``/``downgrade`` for the same reason as the previous merges: a
merge revision that does work cannot be reverted independently of the merge.

Revision ID: a5f2c8d91e3b
Revises: c9d4e1f7a2b8, e2c91b7a5d04
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a5f2c8d91e3b"
down_revision: str | Sequence[str] | None = ("c9d4e1f7a2b8", "e2c91b7a5d04")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Nothing to undo."""

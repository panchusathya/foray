"""Rejoin the candidate-memory-vault line with the pitch/apply merge.

Third round of the same story: PR #107 (candidate memory vault, b8e12a4c7f1d)
and PR #108 (voice intake, whose merge revision b2c3d4e5f6a7 rejoined the
pitch and apply tracks) both parented onto browser_profiles (f4c8a29d71e3)
and merged to main without seeing each other, leaving two heads. The vault
tables are new and disjoint from both tracks, so this adds no schema and
exists only to give ``alembic upgrade head`` one head.

Empty ``upgrade``/``downgrade`` for the same reason as the previous merges: a
merge revision that does work cannot be reverted independently of the merge.

Revision ID: c9d4e1f7a2b8
Revises: b2c3d4e5f6a7, b8e12a4c7f1d
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c9d4e1f7a2b8"
down_revision: str | Sequence[str] | None = ("b2c3d4e5f6a7", "b8e12a4c7f1d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Nothing to undo."""

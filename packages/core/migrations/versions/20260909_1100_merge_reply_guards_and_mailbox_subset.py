"""Rejoin the reply-guard columns with the mailbox-subset table.

PR #123 (rotate over a chosen subset of mailboxes, a1c6e3f92d70) and PR #126
(reply gating: replied_at / follow_up_after / snoozed_until on enrollments,
b1e6d4a9c2f7) both parented onto intake_budget_anchor (f3a9d7c2b481) and
merged without seeing each other, leaving two heads. One adds a join table,
the other adds nullable columns to enrollments; nothing overlaps, so this
adds no schema and exists only to give ``alembic upgrade head`` one head.

Empty ``upgrade``/``downgrade`` for the same reason as the previous merges: a
merge revision that does work cannot be reverted independently of the merge.

Revision ID: d7b2e9c4a1f6
Revises: a1c6e3f92d70, b1e6d4a9c2f7
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "d7b2e9c4a1f6"
down_revision: str | Sequence[str] | None = ("a1c6e3f92d70", "b1e6d4a9c2f7")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Nothing to undo."""

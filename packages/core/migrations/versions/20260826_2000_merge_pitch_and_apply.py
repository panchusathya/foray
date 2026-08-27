"""Rejoin main's apply-track work with the match-pitch line, again.

The same situation `20260826_1900_merge_match_pitch` resolved, one round later:
while the voice branch carried that merge revision, main grew
``browser_profiles`` (f4c8a29d71e3, parented onto referrals), so merging main
back in produced two heads a second time. The branches remain disjoint --
browser profiles touch the apply track, the pitch touches intake -- so this
adds no schema and exists only to give ``alembic upgrade head`` one head.

Empty ``upgrade``/``downgrade`` for the same reason as last time: a merge
revision that does work cannot be reverted independently of the merge.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6, f4c8a29d71e3
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = ("a1b2c3d4e5f6", "f4c8a29d71e3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Nothing to undo."""

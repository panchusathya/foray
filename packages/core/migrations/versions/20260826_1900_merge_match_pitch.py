"""Rejoin the match-pitch line with main.

Two heads, both real, both already applied nowhere in production. The voice
match pitch (`intake_call_matches_offered` -> `intake_text_verification`) was
cut from `b7c2e94a1d05` while main carried on through managed inbox, job-card
assets, portal link codes and referrals to `f3c9a2e64d17`. Neither branch
touches the other's tables, so this merge adds no schema of its own -- it exists
so `alembic upgrade head` has one head to name again.

Empty ``upgrade``/``downgrade`` on purpose. A merge revision that does work is
a revision that cannot be reverted independently of the merge, and there is
nothing to do here: the two branches are disjoint.

Revision ID: a1b2c3d4e5f6
Revises: d4f8a2c6e901, f3c9a2e64d17
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = ("d4f8a2c6e901", "f3c9a2e64d17")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Nothing to do: the branches are disjoint."""


def downgrade() -> None:
    """Nothing to undo."""

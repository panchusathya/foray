"""Add the single-column indexes candidate_preference_facts declared but never got.

The vault migration (b8e12a4c7f1d) created the table with one composite index
and skipped the three ``index=True`` columns on the model, so
``test_migrated_schema_matches_the_models`` has been red since it landed. The
migration has already run in prod, so the fix is this follow-up rather than an
edit to history.

Revision ID: d7e4b2a1c9f6
Revises: a5f2c8d91e3b
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d7e4b2a1c9f6"
down_revision: str | Sequence[str] | None = "a5f2c8d91e3b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEXES = (
    ("ix_candidate_preference_facts_org_id", "org_id"),
    ("ix_candidate_preference_facts_candidate_id", "candidate_id"),
    (
        "ix_candidate_preference_facts_candidate_preference_id",
        "candidate_preference_id",
    ),
)


def upgrade() -> None:
    for name, column in _INDEXES:
        op.create_index(name, "candidate_preference_facts", [column])


def downgrade() -> None:
    for name, _ in _INDEXES:
        op.drop_index(name, table_name="candidate_preference_facts")

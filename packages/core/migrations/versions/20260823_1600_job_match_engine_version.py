"""Engine version on job_match_watermarks.

A finished intake call writes CandidatePreference and does not touch
Candidate.updated_at. The matcher used to key enqueue and the tick on
that candidate clock, so extract was a silent no-op. Bumping
engine_version rescores the existing book without nulling watermarks.

Revision ID: c8e3f91d24b0
Revises: b4d2e80c17a9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e3f91d24b0"
down_revision: str | None = "b4d2e80c17a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_match_watermarks") as batch:
        batch.add_column(
            sa.Column("engine_version", sa.String(length=32), server_default="", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("job_match_watermarks") as batch:
        batch.drop_column("engine_version")

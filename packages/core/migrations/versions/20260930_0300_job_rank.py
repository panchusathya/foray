"""job rank

A send's place in line. The outreach tick wakes the best-ranked few and the
claim breaks run_at ties on it, so the Outbox can reorder what goes next and
the order survives any number of deferrals.

Existing queued sends are ranked by when they were queued, which is the order
they would have gone anyway.

Revision ID: f3a9d2b7c1e4
Revises: e8b2c5d1f4a6
Create Date: 2026-09-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f3a9d2b7c1e4"
down_revision = "e8b2c5d1f4a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("rank", sa.Float(), nullable=True))
    op.execute(
        """
        update jobs set rank = ranked.n
        from (
            select id, row_number() over (
                partition by org_id order by created_at, id
            ) as n
            from jobs
            where kind = 'outreach.send' and status in ('queued', 'running')
        ) as ranked
        where jobs.id = ranked.id
        """
    )


def downgrade() -> None:
    op.drop_column("jobs", "rank")

"""The application form's own questions, on the posting that carries the form.

Apply-by-link ingests the page a candidate texted; when the ATS publishes the
form (Greenhouse does), the real questions land here so the package answers
what the employer actually asks rather than only our standard four. A list of
``{key, question, required, kind, self_identify, options}`` dicts; empty for
every posting whose form we cannot see.

Revision ID: e2c91b7a5d04
Revises: b3f7a06d94c2
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2c91b7a5d04"
down_revision: str | None = "b3f7a06d94c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("job_postings") as batch:
        batch.add_column(
            sa.Column("form_questions", sa.JSON(), nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("job_postings") as batch:
        batch.drop_column("form_questions")

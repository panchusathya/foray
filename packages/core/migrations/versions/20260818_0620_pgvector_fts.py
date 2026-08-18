"""pgvector + generated tsvector for hybrid retrieval.

Revision ID: 7c4b2a19f0e3
Revises: 94e8e8ccab69
Create Date: 2026-08-18 06:20:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from juicebox.search.schema import GIN_INDEX_DDL, TSVECTOR_DDL

revision: str = "7c4b2a19f0e3"
down_revision: str | None = "94e8e8ccab69"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    # JSON array -> pgvector. Empty arrays become NULL (no useful direction).
    op.execute(
        sa.text(
            """
            ALTER TABLE candidates
            ALTER COLUMN embedding TYPE vector
            USING (
                CASE
                    WHEN embedding IS NULL THEN NULL
                    WHEN json_typeof(embedding::json) <> 'array' THEN NULL
                    WHEN json_array_length(embedding::json) = 0 THEN NULL
                    ELSE (
                        SELECT array_agg(trim(elem)::float ORDER BY ord)::vector
                        FROM json_array_elements_text(embedding::json)
                            WITH ORDINALITY AS t(elem, ord)
                    )
                END
            )
            """
        )
    )
    op.execute(sa.text(TSVECTOR_DDL))
    op.execute(sa.text(GIN_INDEX_DDL))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text("DROP INDEX IF EXISTS ix_candidates_search_tsv"))
    op.execute(sa.text("ALTER TABLE candidates DROP COLUMN IF EXISTS search_tsv"))
    op.execute(
        sa.text(
            """
            ALTER TABLE candidates
            ALTER COLUMN embedding TYPE json
            USING CASE
                WHEN embedding IS NULL THEN NULL
                ELSE embedding::text::json
            END
            """
        )
    )

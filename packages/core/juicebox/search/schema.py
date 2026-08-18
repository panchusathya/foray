"""Postgres-only search schema: pgvector extension, generated tsvector, indexes.

Kept out of the SQLAlchemy model so SQLite ``create_all`` stays simple. Called
from ``db.create_all`` (tests against Postgres) and from the Alembic revision
so the two paths cannot drift.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

# ``simple`` matches juicebox.llm.embeddings.tokenize: no stemming, so "go"
# and "pytorch" stay literal tokens the way the in-process BM25 index sees them.
TSVECTOR_DDL = """
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS search_tsv tsvector
GENERATED ALWAYS AS (to_tsvector('simple', coalesce(doc_text, ''))) STORED
"""

GIN_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS ix_candidates_search_tsv
ON candidates USING gin (search_tsv)
"""

# HNSW needs a fixed vector dimension. The embedding column is unbounded so
# hashing (512) and Voyage (typically 1024) can share a schema; cosine search
# is still correct, it just sequential-scans until a later migration pins dim.


def prepare_postgres(engine: Engine) -> None:
    """Idempotent. Safe to call on an empty database or one that already has these."""
    if engine.dialect.name != "postgresql":
        return
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
        if not exists:
            conn.execute(text("CREATE EXTENSION vector"))
        conn.commit()
    with engine.begin() as conn:
        conn.execute(text(TSVECTOR_DDL))
        conn.execute(text(GIN_INDEX_DDL))


def drop_postgres_search(engine: Engine) -> None:
    """Drop search-only artifacts. The embedding column type is the model's job."""
    if engine.dialect.name != "postgresql":
        return
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_candidates_search_tsv"))
        conn.execute(text("ALTER TABLE IF EXISTS candidates DROP COLUMN IF EXISTS search_tsv"))

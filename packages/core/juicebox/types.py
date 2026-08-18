"""Dialect-aware embedding column: JSON on SQLite, pgvector on PostgreSQL."""

from __future__ import annotations

from sqlalchemy import JSON, TypeDecorator


class EmbeddingVector(TypeDecorator):
    """Store a float list as JSON locally and as a ``vector`` in Postgres.

    Dimension is left unbounded so hashing (512) and Voyage (typically 1024)
    can coexist across a ``juicebox reembed`` without a schema change.
    """

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from pgvector.sqlalchemy import Vector

            return dialect.type_descriptor(Vector())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return [float(x) for x in value]

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return [float(x) for x in value]

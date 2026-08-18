"""Alembic environment.

The URL comes from ``juicebox.config`` rather than alembic.ini, so `alembic
upgrade head` always targets the same database the app would open.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import pool

from juicebox.config import get_settings
from juicebox.db import get_engine
from juicebox.models import Base

config = context.config
target_metadata = Base.metadata

# Postgres-only search artifacts live in juicebox.search.schema, not the ORM,
# so autogenerate / `alembic check` must not try to drop them.
_PASSTHROUGH_COLUMNS = {"search_tsv"}
_PASSTHROUGH_INDEXES = {"ix_candidates_search_tsv"}


def include_object(object_, name, type_, reflected, compare_to) -> bool:
    if type_ == "column":
        return name not in _PASSTHROUGH_COLUMNS
    if type_ == "index":
        return name not in _PASSTHROUGH_INDEXES
    return True


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
        # SQLite cannot ALTER most things; batch mode rewrites the table instead.
        render_as_batch=_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Reuse the app's engine builder so SQLite pragmas and pool settings match.
    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            include_object=include_object,
            render_as_batch=connection.dialect.name == "sqlite",
            poolclass=pool.NullPool,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

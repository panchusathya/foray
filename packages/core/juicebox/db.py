"""Engine / session wiring. Works on SQLite (default) and PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import get_settings

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def _build_engine(url: str) -> Engine:
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # Make sure the parent directory exists for file-backed sqlite.
        path_part = url.split("sqlite:///", 1)[-1]
        if path_part and path_part != ":memory:":
            Path(path_part).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver glue
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    else:

        @event.listens_for(engine, "connect")
        def _register_pgvector(dbapi_connection, _record):  # pragma: no cover - driver glue
            try:
                from pgvector.psycopg import register_vector
            except ImportError:
                return
            # The vector extension is created in create_all / migrate; the
            # first connect of a brand-new database happens before that.
            with suppress(Exception):
                register_vector(dbapi_connection)

    return engine


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        _engine = _build_engine(get_settings().database_url)
        _SessionFactory = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


def reset_engine() -> None:
    """Drop the cached engine — used by tests that swap DATABASE_URL."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all() -> None:
    from . import models  # noqa: F401  (registers mappers)
    from .models import Base
    from .search.schema import prepare_postgres

    engine = get_engine()
    if not get_settings().is_sqlite:
        # pgvector's type must exist before CREATE TABLE ... embedding vector.
        from sqlalchemy import text

        with engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            if not exists:
                conn.execute(text("CREATE EXTENSION vector"))
            conn.commit()
            with suppress(Exception):
                from pgvector.psycopg import register_vector

                register_vector(conn.connection.dbapi_connection)
    Base.metadata.create_all(bind=engine)
    prepare_postgres(engine)


def drop_all() -> None:
    from .models import Base
    from .search.schema import drop_postgres_search

    engine = get_engine()
    drop_postgres_search(engine)
    Base.metadata.drop_all(bind=engine)

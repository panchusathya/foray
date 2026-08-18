"""Programmatic Alembic access.

The config is built in code rather than read from alembic.ini so that
``juicebox migrate`` behaves identically however the package was installed, and
so there is exactly one place that knows where the migration scripts live.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from .db import get_engine

logger = logging.getLogger(__name__)

CORE_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = CORE_ROOT / "migrations"


class MigrationsUnavailable(RuntimeError):
    """The migration scripts are not on disk (e.g. a partial install)."""


def alembic_config() -> Config:
    if not (MIGRATIONS_DIR / "env.py").exists():
        raise MigrationsUnavailable(f"no migrations found at {MIGRATIONS_DIR}")
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    # env.py reads DATABASE_URL via juicebox.config; nothing else needs the URL.
    return config


def head_revision() -> str | None:
    script = ScriptDirectory.from_config(alembic_config())
    return script.get_current_head()


def current_revision() -> str | None:
    with get_engine().connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def is_up_to_date() -> bool:
    try:
        return current_revision() == head_revision()
    except MigrationsUnavailable:
        return False


def upgrade(revision: str = "head") -> None:
    command.upgrade(alembic_config(), revision)


def downgrade(revision: str) -> None:
    command.downgrade(alembic_config(), revision)


def stamp(revision: str = "head") -> None:
    command.stamp(alembic_config(), revision)


def ensure_schema() -> str:
    """Bring the database to head. Returns what it did, for logging.

    Falls back to ``create_all`` only when the migration scripts are missing,
    which should not happen in a normal install but should not brick a demo.
    """
    from .db import create_all

    try:
        upgrade()
    except MigrationsUnavailable as exc:
        logger.warning("%s — creating tables directly instead", exc)
        create_all()
        return "create_all"
    return "migrated"

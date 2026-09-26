"""The migrations must stay in step with the models.

Without this, the first person to add a column and forget the migration finds
out in production. Runs on SQLite so it needs no infrastructure; the Postgres
equivalent is `alembic check` in CI.
"""

from __future__ import annotations

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext


@pytest.fixture
def migrated_db(tmp_path, monkeypatch):
    """A database built purely by migrations, not by create_all."""
    from juicebox import db as db_module
    from juicebox import migrate
    from juicebox.config import reset_settings_cache

    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset_settings_cache()
    db_module.reset_engine()

    migrate.upgrade()
    yield migrate

    db_module.reset_engine()
    reset_settings_cache()


class TestMigrations:
    def test_upgrade_head_reaches_the_latest_revision(self, migrated_db):
        assert migrated_db.current_revision() == migrated_db.head_revision()
        assert migrated_db.is_up_to_date()

    def test_migrated_schema_matches_the_models(self, migrated_db):
        """Fails when models change without a matching migration."""
        from juicebox.db import get_engine
        from juicebox.models import Base

        with get_engine().connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"compare_type": True, "target_metadata": Base.metadata}
            )
            diff = compare_metadata(context, Base.metadata)

        assert diff == [], f"models and migrations have drifted: {diff}"

    def test_every_model_table_exists(self, migrated_db):
        from sqlalchemy import inspect

        from juicebox.db import get_engine
        from juicebox.models import Base

        present = set(inspect(get_engine()).get_table_names())
        expected = set(Base.metadata.tables)
        assert expected <= present, f"missing tables: {sorted(expected - present)}"

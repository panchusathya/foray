"""Settings validation: development stays frictionless, production fails fast."""

from __future__ import annotations

import pytest

from juicebox.config import (
    DEFAULT_PBKDF2_ROUNDS,
    DEV_SECRET_KEY,
    ConfigError,
    get_settings,
    reset_settings_cache,
)


@pytest.fixture
def settings_for(monkeypatch):
    """Build settings from an explicit environment."""

    def build(**env):
        for key, value in env.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, value)
        reset_settings_cache()
        return get_settings()

    yield build
    reset_settings_cache()


class TestProductionGuard:
    def test_default_secret_is_rejected_in_production(self, settings_for):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            settings_for(JUICEBOX_ENV="production", SECRET_KEY=DEV_SECRET_KEY)

    def test_empty_secret_is_rejected_in_production(self, settings_for):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            settings_for(JUICEBOX_ENV="production", SECRET_KEY="")

    def test_common_placeholders_are_rejected(self, settings_for):
        for placeholder in ("changeme", "change-me", "secret", "test-secret"):
            with pytest.raises(ConfigError, match="SECRET_KEY"):
                settings_for(JUICEBOX_ENV="production", SECRET_KEY=placeholder)

    def test_short_secret_is_rejected(self, settings_for):
        with pytest.raises(ConfigError, match="at least 32"):
            settings_for(JUICEBOX_ENV="production", SECRET_KEY="abc123")

    def test_prod_alias_is_also_guarded(self, settings_for):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            settings_for(JUICEBOX_ENV="prod", SECRET_KEY=DEV_SECRET_KEY)

    def test_staging_is_not_production_but_still_rejects_the_default_secret(self, settings_for):
        with pytest.raises(ConfigError, match="SECRET_KEY"):
            settings_for(JUICEBOX_ENV="staging", SECRET_KEY=DEV_SECRET_KEY)
        settings = settings_for(
            JUICEBOX_ENV="staging",
            SECRET_KEY="x" * 48,
            JUICEBOX_PBKDF2_ROUNDS=str(DEFAULT_PBKDF2_ROUNDS),
        )
        assert settings.is_production is False
        assert settings.requires_secure_secret is True


class TestPbkdf2Rounds:

    def test_production_rejects_the_test_cost(self, settings_for):
        with pytest.raises(ConfigError, match="JUICEBOX_PBKDF2_ROUNDS"):
            settings_for(
                JUICEBOX_ENV="production",
                SECRET_KEY="x" * 48,
                JUICEBOX_PBKDF2_ROUNDS="1000",
            )

    def test_staging_rejects_the_test_cost(self, settings_for):
        with pytest.raises(ConfigError, match="JUICEBOX_PBKDF2_ROUNDS"):
            settings_for(
                JUICEBOX_ENV="staging",
                SECRET_KEY="x" * 48,
                JUICEBOX_PBKDF2_ROUNDS="1000",
            )


class TestRetrievalBackend:
    def test_auto_follows_the_database_dialect(self, settings_for):
        sqlite = settings_for(RETRIEVAL_BACKEND="auto", DATABASE_URL="sqlite:///tmp/x.db")
        assert sqlite.uses_postgres_retrieval is False
        postgres = settings_for(
            RETRIEVAL_BACKEND="auto",
            DATABASE_URL="postgresql+psycopg://juicebox:juicebox@localhost/juicebox",
        )
        assert postgres.uses_postgres_retrieval is True

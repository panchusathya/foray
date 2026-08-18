"""Settings validation: development stays frictionless, production fails fast."""

from __future__ import annotations

import pytest

from juicebox.config import DEV_SECRET_KEY, ConfigError, get_settings, reset_settings_cache


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

    def test_a_real_secret_boots(self, settings_for):
        settings = settings_for(JUICEBOX_ENV="production", SECRET_KEY="x" * 48)
        assert settings.is_production
        assert settings.secret_key == "x" * 48


class TestDevelopmentStaysEasy:
    def test_development_accepts_the_default_secret(self, settings_for):
        settings = settings_for(JUICEBOX_ENV="development", SECRET_KEY=DEV_SECRET_KEY)
        assert settings.is_production is False
        assert settings.secret_key == DEV_SECRET_KEY

    def test_no_env_at_all_still_boots(self, settings_for):
        settings = settings_for(JUICEBOX_ENV=None, SECRET_KEY=None)
        assert settings.env == "development"
        assert settings.secret_key == DEV_SECRET_KEY

    def test_docker_compose_default_is_not_production(self, settings_for):
        """docker compose up must keep working out of the box."""
        settings = settings_for(JUICEBOX_ENV=None, SECRET_KEY=DEV_SECRET_KEY)
        settings.validate()  # no raise


class TestRetrievalBackend:
    def test_auto_follows_the_database_dialect(self, settings_for):
        sqlite = settings_for(RETRIEVAL_BACKEND="auto", DATABASE_URL="sqlite:///tmp/x.db")
        assert sqlite.uses_postgres_retrieval is False
        postgres = settings_for(
            RETRIEVAL_BACKEND="auto",
            DATABASE_URL="postgresql+psycopg://juicebox:juicebox@localhost/juicebox",
        )
        assert postgres.uses_postgres_retrieval is True

    def test_memory_overrides_postgres(self, settings_for):
        settings = settings_for(
            RETRIEVAL_BACKEND="memory",
            DATABASE_URL="postgresql+psycopg://juicebox:juicebox@localhost/juicebox",
        )
        assert settings.uses_postgres_retrieval is False

    def test_postgres_can_be_forced(self, settings_for):
        settings = settings_for(
            RETRIEVAL_BACKEND="postgres", DATABASE_URL="sqlite:///tmp/x.db"
        )
        assert settings.uses_postgres_retrieval is True

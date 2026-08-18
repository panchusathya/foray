from __future__ import annotations

import pytest

from foray.config import Settings, settings


def test_database_url_default_is_foray():
    # The acceptance check: importing settings and reading database_url works.
    assert "foray" in settings.database_url
    assert settings.embedding_dim == 384


def test_require_fails_fast_on_missing_key():
    s = Settings(_env_file=None)
    s.github_token = ""
    with pytest.raises(SystemExit) as exc:
        s.require("github_token")
    assert "GITHUB_TOKEN" in str(exc.value)


def test_require_passes_when_present():
    s = Settings(_env_file=None)
    s.github_token = "ghp_xxx"
    s.require("github_token")  # should not raise

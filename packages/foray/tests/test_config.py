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


def test_stale_score_weight_env_is_ignored(monkeypatch):
    """Renaming FORAY_SCORE_WEIGHT -> FORAY_W_SCORE means a deployed .env
    carrying the old name must not silently dominate the three-term blend."""
    monkeypatch.setenv("FORAY_SCORE_WEIGHT", "0.7")
    monkeypatch.setenv("FORAY_VECTOR_WEIGHT", "0.3")
    fresh = Settings(_env_file=None)
    assert fresh.w_score == 0.3
    assert fresh.w_vector == 0.2
    assert fresh.w_rrf == 0.5
    assert fresh.retrieval_arms == "vector,lexical"
    assert fresh.unscored_score == 0
    assert fresh.rerank_enabled is False

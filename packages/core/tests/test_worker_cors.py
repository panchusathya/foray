"""CORS: the one combination the app must never be able to serve.

``allow_origins=["*"]`` with ``allow_credentials=True`` tells every origin on
the internet that it may send this API cookies and Authorization headers. An
unset CORS_ORIGINS used to land exactly there, so the deployment that simply
forgot the variable got the most permissive setting available.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from juicebox.config import reset_settings_cache


@pytest.fixture
def app_with_origins(monkeypatch):
    def build(value):
        monkeypatch.setenv("CORS_ORIGINS", value)
        reset_settings_cache()
        from juicebox.main import create_app

        return TestClient(create_app())

    yield build
    reset_settings_cache()


def _preflight(client, origin):
    return client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


class TestCorsFallsBackClosed:
    def test_an_empty_origin_list_never_becomes_a_credentialed_wildcard(
        self, app_with_origins
    ):
        client = app_with_origins("")
        response = _preflight(client, "https://evil.example")

        allowed = response.headers.get("access-control-allow-origin", "")
        assert allowed != "*"
        assert not allowed
        assert "access-control-allow-credentials" not in response.headers

    def test_an_explicit_wildcard_loses_credentials_rather_than_keeping_both(
        self, app_with_origins
    ):
        client = app_with_origins("*")
        response = _preflight(client, "https://evil.example")

        assert response.headers.get("access-control-allow-origin") == "*"
        assert "access-control-allow-credentials" not in response.headers

    def test_a_configured_origin_still_gets_credentials(self, app_with_origins):
        client = app_with_origins("https://app.goforay.io,http://localhost:5173")
        response = _preflight(client, "https://app.goforay.io")

        assert response.headers["access-control-allow-origin"] == "https://app.goforay.io"
        assert response.headers["access-control-allow-credentials"] == "true"

        other = _preflight(client, "https://evil.example")
        assert other.headers.get("access-control-allow-origin", "") != "*"

"""Live Microsoft identity contract — skipped unless Graph app credentials exist.

Does not send mail. Token acquisition is enough to prove the request shape
(client_id, client_secret, scope=.default) is accepted by the tenant.
"""

from __future__ import annotations

import os

import pytest

from juicebox.outreach.graph import HttpGraphTransport, client_credentials_token

pytestmark = pytest.mark.live

GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")

needs_graph_app = pytest.mark.skipif(
    not GRAPH_CLIENT_ID
    or not GRAPH_CLIENT_SECRET
    or not GRAPH_TENANT_ID
    or GRAPH_TENANT_ID.lower() in {"common", "organizations", "consumers"},
    reason="GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, and a real GRAPH_TENANT_ID are required",
)


@pytest.fixture
def live_graph_env(monkeypatch):
    monkeypatch.setenv("GRAPH_CLIENT_ID", GRAPH_CLIENT_ID)
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", GRAPH_CLIENT_SECRET)
    monkeypatch.setenv("GRAPH_TENANT_ID", GRAPH_TENANT_ID)

    from juicebox.config import reset_settings_cache
    from juicebox.outreach.graph import reset_app_token_cache, set_graph_transport

    reset_settings_cache()
    reset_app_token_cache()
    set_graph_transport(None)
    yield
    reset_settings_cache()
    reset_app_token_cache()
    set_graph_transport(None)


@needs_graph_app
class TestGraphContract:
    def test_client_credentials_token_is_accepted(self, live_graph_env):
        payload = client_credentials_token(HttpGraphTransport())
        assert payload.get("access_token")
        assert payload.get("token_type", "").lower() == "bearer"

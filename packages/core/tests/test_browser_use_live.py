"""Wire contract against real Browser Use Cloud.

Marked ``live`` and excluded from the default run. Creating a session spends
credits, so this only runs when ``BROWSER_USE_API_KEY`` is set:

    pytest -m live tests/test_browser_use_live.py
"""

from __future__ import annotations

import os

import pytest

from juicebox.applications.submit.browser import cloud as browser_cloud

KEY = os.environ.get("BROWSER_USE_API_KEY", "")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not KEY, reason="BROWSER_USE_API_KEY is not set"),
]


@pytest.fixture
def live_key(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", KEY)
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


def test_create_get_and_stop_a_session(live_key):
    session = browser_cloud.create_session(
        metadata={"phase": "live-test"},
        timeout_min=1,
    )
    try:
        assert session.id
        assert session.cdp_url
        held = browser_cloud.get_session(session.id)
        assert held is not None
        assert held.id == session.id
        assert held.status == "active"
    finally:
        browser_cloud.stop_session(session.id)
    stopped = browser_cloud.get_session(session.id)
    assert stopped is None or stopped.status == "stopped"

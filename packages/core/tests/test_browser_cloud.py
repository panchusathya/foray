"""Browser Use Cloud client: create, stop, 429, injectable fake."""

from __future__ import annotations

import pytest

from juicebox.applications.submit.base import SubmitDeferred
from juicebox.applications.submit.browser import cloud as browser_cloud
from juicebox.applications.submit.browser.cloud import (
    FakeCloudClient,
    TooManySessions,
    set_cloud_client,
)
from juicebox.config import reset_settings_cache


@pytest.fixture
def fake_cloud(monkeypatch):
    monkeypatch.setenv("BROWSER_USE_API_KEY", "bu_test")
    reset_settings_cache()
    fake = FakeCloudClient()
    set_cloud_client(fake)
    yield fake
    set_cloud_client(None)
    reset_settings_cache()


class TestCloudClient:
    def test_create_returns_cdp_url(self, fake_cloud):
        session = browser_cloud.create_session(metadata={"org_id": "o1", "phase": "live"})
        assert session.id.startswith("bu-")
        assert session.cdp_url.startswith("ws://")
        assert fake_cloud.created[0]["proxyCountryCode"] == "us"
        assert fake_cloud.created[0]["timeout"] == 15
        assert fake_cloud.created[0]["enableRecording"] is False
        assert fake_cloud.created[0]["metadata"]["org_id"] == "o1"

class TestDriverStopsOwnedSession:
    def test_driver_stops_a_session_it_created(self, fake_cloud, monkeypatch):
        from juicebox.applications.submit.browser import driver

        calls = {"connect": None}

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = []

            def set_default_timeout(self, _):
                pass

            def set_default_navigation_timeout(self, _):
                pass

            def new_page(self):
                return FakePage()

        class FakeBrowser:
            def __init__(self):
                self.contexts = [FakeContext()]

            def close(self):
                pass

        class FakeChromium:
            def connect_over_cdp(self, url, timeout=None):
                calls["connect"] = url
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        import sys
        import types

        fake_mod = types.ModuleType("playwright.sync_api")
        fake_mod.sync_playwright = lambda: FakePlaywright()
        pkg = types.ModuleType("playwright")
        pkg.sync_api = fake_mod
        monkeypatch.setitem(sys.modules, "playwright", pkg)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_mod)
        monkeypatch.setattr(driver, "available", lambda: (True, ""))
        monkeypatch.setattr(driver, "_MIN_INTERVAL_SECONDS", 0)

        log = driver.RunLog()
        with driver.session(log=log) as page:
            assert page is not None
        assert calls["connect"].startswith("ws://browser-use.test/")
        assert fake_cloud.stopped
        assert any("stopped" in s for s in log.steps)
        assert any("managed chrome" in s for s in log.steps)


class TestLoopDefersOn429:
    def test_too_many_sessions_becomes_submit_deferred(self, fake_cloud, monkeypatch):
        from juicebox.applications.ats.detect import ApplyTarget
        from juicebox.applications.submit.base import SubmissionContext
        from juicebox.applications.submit.browser.agent import loop

        fake_cloud.fail_create = TooManySessions("full")
        ctx = SubmissionContext(
            application_id="a1",
            org_id="o1",
            candidate_id="c1",
            target=ApplyTarget(vendor="greenhouse", apply_url="https://boards.greenhouse.io/x/1"),
            posting_url="https://boards.greenhouse.io/x/1",
        )
        with pytest.raises(SubmitDeferred):
            loop.run(ctx, allow_submit=True)

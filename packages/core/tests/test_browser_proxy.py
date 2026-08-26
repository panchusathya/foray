"""Turning a proxy URL into what Playwright wants.

A residential proxy is the answer to a datacentre IP being flagged as a bot,
and the URL a vendor hands you (`http://user:pass@gate:7777`) is not the shape
Playwright's `proxy=` takes -- it wants the server and the credentials apart.
This is where they come apart, so it is where the tests are.
"""

from __future__ import annotations

from juicebox.applications.submit.browser.driver import _proxy_config


def test_empty_is_direct():
    assert _proxy_config("") is None
    assert _proxy_config("   ") is None


def test_credentialed_url_splits_server_from_auth():
    config = _proxy_config("http://user123:secretpass@gate.provider.com:7777")
    assert config == {
        "server": "http://gate.provider.com:7777",
        "username": "user123",
        "password": "secretpass",
    }


def test_a_bare_host_port_is_assumed_http():
    assert _proxy_config("gate.provider.com:7777") == {
        "server": "http://gate.provider.com:7777"
    }


def test_no_credentials_means_no_auth_keys():
    config = _proxy_config("http://gate.provider.com:7777")
    assert config == {"server": "http://gate.provider.com:7777"}
    assert "username" not in config


def test_scheme_is_preserved():
    """Some vendors front their gateway with https or socks5."""
    assert _proxy_config("socks5://u:p@host:1080")["server"] == "socks5://host:1080"


def test_a_password_with_url_characters_survives():
    """Rotating-session vendors put the session token in the password with
    hyphens and dashes; those are not delimiters here."""
    config = _proxy_config("http://user-session-abc:p-a-s-s@host:9999")
    assert config["username"] == "user-session-abc"
    assert config["password"] == "p-a-s-s"


def test_garbage_is_direct_not_a_crash():
    assert _proxy_config("not a url") is None


class TestStickySession:
    """A form fill is several requests; they must all leave from one IP."""

    BD = "http://brd-customer-x-zone-isp:pw@brd.superproxy.io:44445"

    def test_a_session_token_is_appended_for_a_session_proxy(self):
        config = _proxy_config(self.BD, session_token="abc123")
        assert config["username"] == "brd-customer-x-zone-isp-session-abc123"

    def test_no_token_leaves_the_username_alone(self):
        config = _proxy_config(self.BD)
        assert config["username"] == "brd-customer-x-zone-isp"

    def test_a_configured_session_is_not_overridden(self):
        """An operator who pinned one IP on purpose keeps it."""
        url = "http://brd-customer-x-zone-isp-session-fixed:pw@brd.superproxy.io:44445"
        config = _proxy_config(url, session_token="abc123")
        assert config["username"] == "brd-customer-x-zone-isp-session-fixed"

    def test_country_targeting_in_the_username_is_preserved(self):
        url = "http://brd-customer-x-zone-isp-country-us:pw@brd.superproxy.io:44445"
        config = _proxy_config(url, session_token="abc123")
        assert config["username"] == "brd-customer-x-zone-isp-country-us-session-abc123"

    def test_a_non_session_proxy_is_untouched(self):
        """A plain proxy does not understand -session- syntax."""
        config = _proxy_config("http://user:pw@gate.otherproxy.com:8000", session_token="abc123")
        assert config["username"] == "user"


class TestScrapingBrowserMode:
    """When a CDP endpoint is set, the driver connects to a managed browser
    instead of launching local Chromium, and adds none of its own stealth."""

    def _run_session(self, monkeypatch, *, cdp_url, proxy_url=""):
        """Drive driver.session with a fake sync_playwright, capturing which
        launch path was taken."""
        from juicebox.applications.submit.browser import driver
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("APPLY_BROWSER_CDP_URL", cdp_url)
        monkeypatch.setenv("APPLY_PROXY_URL", proxy_url)
        reset_settings_cache()

        calls = {"connect_over_cdp": None, "launch": None}

        class FakePage:
            pass

        class FakeContext:
            def __init__(self):
                self.pages = []
                self.init_scripts = []

            def set_default_timeout(self, _):
                pass

            def set_default_navigation_timeout(self, _):
                pass

            def add_init_script(self, script):
                self.init_scripts.append(script)

            def new_page(self):
                page = FakePage()
                self.pages.append(page)
                return page

        class FakeBrowser:
            def __init__(self, remote):
                self.contexts = [FakeContext()] if remote else []
                self._remote = remote

            def new_context(self, **kwargs):
                self._ctx_kwargs = kwargs
                ctx = FakeContext()
                self.contexts.append(ctx)
                return ctx

            def close(self):
                pass

        class FakeChromium:
            def connect_over_cdp(self, url, timeout=None):
                calls["connect_over_cdp"] = url
                return FakeBrowser(remote=True)

            def launch(self, **kwargs):
                calls["launch"] = kwargs
                return FakeBrowser(remote=False)

        class FakePlaywright:
            chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        # driver.session does `from playwright.sync_api import sync_playwright`
        # inside the function, and playwright is not installed locally, so a
        # fake module is registered rather than an attribute patched.
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
        reset_settings_cache()
        return calls, log

    def test_cdp_url_connects_over_cdp(self, monkeypatch):
        calls, log = self._run_session(
            monkeypatch, cdp_url="wss://brd-customer-x-zone-sb:pw@brd.superproxy.io:9222"
        )
        assert calls["connect_over_cdp"] == "wss://brd-customer-x-zone-sb:pw@brd.superproxy.io:9222"
        assert calls["launch"] is None
        assert any("scraping browser" in s for s in log.steps)

    def test_no_cdp_url_launches_local(self, monkeypatch):
        calls, _ = self._run_session(monkeypatch, cdp_url="", proxy_url="")
        assert calls["launch"] is not None
        assert calls["connect_over_cdp"] is None


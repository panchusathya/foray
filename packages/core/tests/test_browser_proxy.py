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

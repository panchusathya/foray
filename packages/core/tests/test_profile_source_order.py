"""Which profile source runs first, and why the browser one now does not.

The scrape was free per lookup and first for that reason. It was not free in
wall-clock: with no LinkedIn session it launched Chromium, spent a 30s
navigation timeout and one retry proving a miss, and only then fell through to
the paid provider that was always going to answer. A candidate mid-intake spent
that minute watching a typing indicator.

These tests pin the resulting rule, which is narrower than "the scrape is
off": what is gated is the *browser*, so an injected scraper is unaffected. Get
that wrong and the switch quietly disables the seam every other intake test
runs on -- which is why it is asserted here rather than left to the flag.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_linkedin_intake import _short_thread

from juicebox.config import get_settings, reset_settings_cache
from juicebox.enrich import linkedin_scrape
from juicebox.enrich.linkedin_profile import (
    FakeProfileProvider,
    set_profile_provider,
)
from juicebox.enrich.linkedin_scrape import (
    LinkedInProfile,
    has_scraper,
    reset_scraper,
    set_scraper,
)
from juicebox.enrich.profile_sources import scrape_first
from juicebox.jobs import KIND_LINKEDIN_SCRAPE
from juicebox.messaging.conversation import run_turn
from juicebox.models import Job
from juicebox.worker import process_job

COOKIE = "li_at=abc123"
ADA_URL = "https://www.linkedin.com/in/ada-lovelace"

PAID_PAYLOAD = {
    "person": {
        "fullName": "Ada Lovelace",
        "headline": "Software Engineer at Stripe",
        "location": "San Francisco",
        "linkedInUrl": ADA_URL,
        "positions": {
            "positionHistory": [
                {
                    "title": "Software Engineer",
                    "companyName": "Stripe",
                    "startEndDate": {"start": {"year": 2018}},
                }
            ]
        },
    }
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """No flag, no cookie, no scraper: the shape of a production worker."""
    monkeypatch.delenv("LINKEDIN_SCRAPE_ENABLED", raising=False)
    monkeypatch.delenv("LINKEDIN_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("LINKEDIN_SESSION_PATH", raising=False)
    reset_settings_cache()
    reset_scraper()
    yield
    reset_scraper()
    set_profile_provider(None)
    reset_settings_cache()


def _profile() -> LinkedInProfile:
    return LinkedInProfile(url=ADA_URL, full_name="Ada Lovelace")


class TestScrapeFirst:
    def test_default_is_paid_first(self):
        """The prod default. Nothing configured means no browser launch."""
        assert scrape_first() is False

    def test_the_flag_alone_does_not_turn_it_on(self, monkeypatch):
        """A flag with no session is a config that cannot succeed.

        Obeying it would restore the exact minute-long guaranteed miss the flag
        exists to remove, so the missing cookie vetoes the flag rather than the
        other way round.
        """
        monkeypatch.setenv("LINKEDIN_SCRAPE_ENABLED", "true")
        reset_settings_cache()
        assert get_settings().linkedin_scrape_enabled is True
        assert scrape_first() is False

    def test_a_session_alone_does_not_turn_it_on(self, monkeypatch):
        """Someone else's cookie in the env is not a decision to pay the wait."""
        monkeypatch.setenv("LINKEDIN_SESSION_COOKIE", COOKIE)
        reset_settings_cache()
        assert scrape_first() is False

    def test_flag_and_session_together_restore_free_first(self, monkeypatch):
        """The escape hatch. Where the scrape can work, the old order is right."""
        monkeypatch.setenv("LINKEDIN_SCRAPE_ENABLED", "true")
        monkeypatch.setenv("LINKEDIN_SESSION_COOKIE", COOKIE)
        reset_settings_cache()
        assert scrape_first() is True

    def test_an_injected_scraper_wins_over_everything(self):
        """The seam is not the browser, so the browser's gate must not catch it.

        No flag and no cookie here -- the state every other intake test runs
        in. If this returns False those tests stop exercising the scrape path
        and start exercising the paid one without saying so.
        """
        set_scraper(lambda url: _profile())
        assert has_scraper() is True
        assert scrape_first() is True

    def test_resetting_the_scraper_puts_the_gate_back(self):
        """A leaked fake must not leave the browser path armed for later tests."""
        set_scraper(lambda url: _profile())
        reset_scraper()
        assert scrape_first() is False


class TestTheHandlerSkipsTheBrowser:
    """The assertion that matters, made where the minute was actually spent."""

    def test_no_browser_launch_and_the_paid_provider_answers(
        self, session, account, fake_channel, monkeypatch
    ):
        """A shut gate must reach the paid provider without touching Chromium.

        ``_playwright_fetch`` is the whole cost, so it is replaced with
        something that fails loudly: reaching it is a test failure rather than
        merely a slow test. The paid provider's own call log is the positive
        half -- skipping the scrape would be no improvement if it also skipped
        the lookup that answers.
        """

        def _explode(*args, **kwargs):
            raise AssertionError("launched a browser with the scrape gate shut")

        monkeypatch.setattr(linkedin_scrape, "_playwright_fetch", _explode)
        provider = FakeProfileProvider({ADA_URL: PAID_PAYLOAD})
        set_profile_provider(provider)

        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        from test_messaging import _say

        _say(session, thread, ADA_URL)
        job = session.scalars(
            select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE)
        ).one()
        process_job(session, job)

        assert provider.calls == [ADA_URL]
        assert (thread.record or {}).get("_linkedin", {}).get("scrape")

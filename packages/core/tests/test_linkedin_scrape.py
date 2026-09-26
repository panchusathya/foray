"""Parse a LinkedIn profile HTML fixture. No network, no Playwright."""

from __future__ import annotations

from pathlib import Path

from juicebox.enrich.linkedin_scrape import (
    is_ic_title,
    looks_like_auth_wall,
    parse_profile_html,
    parse_years_experience,
)
from juicebox.enrich.linkedin_url import canonical_linkedin_url, find_linkedin, linkedin_slug

FIXTURE = Path(__file__).parent / "fixtures" / "linkedin_profile.html"


class TestLinkedinUrl:
    def test_a_pasted_handle_becomes_a_canonical_url(self):
        assert (
            find_linkedin("here: linkedin.com/in/ada-lovelace thanks")
            == "https://www.linkedin.com/in/ada-lovelace"
        )

    def test_https_www_is_accepted(self):
        assert linkedin_slug("https://www.linkedin.com/in/Ada-Lovelace/") == "ada-lovelace"

    def test_a_company_page_is_not_a_profile(self):
        assert canonical_linkedin_url("https://www.linkedin.com/company/stripe") == ""


class TestParseProfileHtml:
    def test_json_ld_and_og_tags_fill_the_profile(self):
        html = FIXTURE.read_text(encoding="utf-8")
        profile = parse_profile_html(html, url="https://www.linkedin.com/in/ada-lovelace")
        assert profile.full_name == "Ada Lovelace"
        assert profile.current_title == "Software Engineer"
        assert profile.current_employer == "Stripe"
        assert "San Francisco" in profile.location
        assert profile.is_ic is True
        assert profile.experience_years is not None
        assert profile.experience_years >= 1

    def test_an_auth_wall_is_detected(self):
        assert looks_like_auth_wall("<html>authwall join to view this profile</html>")

    def test_a_manager_title_is_not_ic(self):
        assert is_ic_title("Engineering Manager") is False
        assert is_ic_title("Staff Software Engineer") is True


class TestYearsMapping:
    def test_years_are_read_from_plain_text(self):
        assert parse_years_experience("i've been doing this 8 years") == 8.0
        assert parse_years_experience("no idea") is None

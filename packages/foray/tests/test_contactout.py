"""ContactOut adapter and the provider seam it plugs into.

The assertions that matter here are about money and about not corrupting the
email table: a lookup must not fire when there is nothing to look up, a source
string must have a place on the confidence ladder before it reaches a check
constraint, and a personal address must not be recorded as a work one.
"""

from __future__ import annotations

import pytest

from foray.enrich import contactout
from foray.enrich.contactout import ContactOutProvider
from foray.enrich.contacts import (
    CONFIDENCE,
    FoundEmail,
    ProviderResult,
    store_result,
)

# The shape the live API actually returns: contacts under `profile`, split into
# `personal_email` / `work_email` / a combined `email`.
LIVE_PAYLOAD = {
    "status_code": 200,
    "profile": {
        "full_name": "Yiheng Fang",
        "email": ["yiheng.fang@gmail.com"],
        "personal_email": ["yiheng.fang@gmail.com"],
        "work_email": [],
        "phone": [],
        "company": {"name": "Meta", "domain": "meta.com"},
    },
}


class StubConn:
    """Records what would have been written."""

    def __init__(self) -> None:
        self.executed: list[tuple] = []

    async def execute(self, *args):
        self.executed.append(args)


class TestNormaliseProfile:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            # Locale subdomains are redirects to the same profile; the provider
            # matches more reliably against the canonical host.
            ("https://au.linkedin.com/in/Kethan-pilla", "https://www.linkedin.com/in/Kethan-pilla"),
            ("https://vn.linkedin.com/in/arielrideout", "https://www.linkedin.com/in/arielrideout"),
            ("https://linkedin.com/in/callen-rain-dev", "https://www.linkedin.com/in/callen-rain-dev"),
            ("https://www.linkedin.com/in/mferris/", "https://www.linkedin.com/in/mferris"),
            ("https://www.linkedin.com/in/x?trk=abc", "https://www.linkedin.com/in/x"),
        ],
    )
    def test_canonicalises_a_profile_url(self, raw, expected):
        assert contactout._normalise_profile(raw) == expected

    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "https://github.com/octocat", "https://linkedin.com/company/meta"]
    )
    def test_rejects_anything_that_is_not_a_profile(self, raw):
        assert contactout._normalise_profile(raw) is None


class TestUsable:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("  Ada@Databricks.COM ", "ada@databricks.com"),
            ("nope", None),
            ("", None),
            (None, None),
            ("two addresses@a.com b@c.com", None),
            # RFC 2606 reserves example.com precisely so it reaches nobody.
            ("ada@example.com", None),
            # The pair ContactOut hands back once its quota is spent.
            ("email1@example.com", None),
            ("email2@gmail.com", None),
        ],
    )
    def test_normalises_or_rejects(self, raw, expected):
        assert contactout._usable(raw) == expected


class TestParse:
    def test_reads_the_live_payload_shape(self):
        result = ContactOutProvider()._parse(LIVE_PAYLOAD)
        assert [e.email for e in result.emails] == ["yiheng.fang@gmail.com"]
        assert result.emails[0].is_work is False
        assert result.employer_domain == "meta.com"

    def test_a_freemail_address_in_the_work_list_is_not_recorded_as_work(self):
        """Providers mislabel. Sending there is fine; calling it a work address
        is not — is_work is frozen at first insert, so a wrong value is
        permanent."""
        result = ContactOutProvider()._parse(
            {"profile": {"work_email": ["someone@gmail.com"], "personal_email": []}}
        )
        assert [(e.email, e.is_work) for e in result.emails] == [("someone@gmail.com", False)]

    def test_a_real_work_domain_is_kept_as_work(self):
        result = ContactOutProvider()._parse(
            {"profile": {"work_email": ["a@databricks.com"], "personal_email": []}}
        )
        assert result.emails[0].is_work is True

    def test_the_same_address_in_both_lists_is_stored_once_as_personal(self):
        result = ContactOutProvider()._parse(
            {
                "profile": {
                    "personal_email": ["dual@gmail.com"],
                    "work_email": ["dual@gmail.com"],
                }
            }
        )
        assert [(e.email, e.is_work) for e in result.emails] == [("dual@gmail.com", False)]

    def test_no_contacts_is_an_empty_result_not_an_error(self):
        result = ContactOutProvider()._parse({"profile": {"full_name": "Nobody"}})
        assert result.emails == []
        assert result.personal == []

    def test_a_website_style_domain_is_reduced_to_a_host(self):
        result = ContactOutProvider()._parse(
            {"profile": {"personal_email": [], "work_email": [],
                         "company": {"website": "https://www.aurora.tech/careers"}}}
        )
        assert result.employer_domain == "aurora.tech"


class TestNoLookupWithoutAnIdentity:
    async def test_a_candidate_with_no_linkedin_url_costs_nothing(self):
        """The provider is paid per address found, but a request with no profile
        cannot match anything — so do not make it."""
        provider = ContactOutProvider(client=object())  # would explode if used
        result = await provider.find_emails({"login": "ghost", "linkedin_url": None})
        assert result.emails == []
        assert provider.calls == 0

    async def test_a_company_url_is_not_a_person(self):
        provider = ContactOutProvider(client=object())
        result = await provider.find_emails(
            {"login": "x", "linkedin_url": "https://linkedin.com/company/meta"}
        )
        assert result.emails == []
        assert provider.calls == 0


class TestStoreResult:
    async def test_writes_each_address_with_its_ladder_confidence(self):
        conn = StubConn()
        written = await store_result(
            conn,
            42,
            "contactout",
            ProviderResult(
                emails=[
                    FoundEmail("me@gmail.com", is_work=False),
                    FoundEmail("me@stripe.com", is_work=True, verified=True),
                ]
            ),
        )
        assert written == 2
        rows = [args for args in conn.executed if "insert into candidate_emails" in args[0]]
        assert len(rows) == 2
        personal, work = rows
        # (sql, candidate_id, email, source, is_work, confidence, verified, evidence, occurrences)
        assert personal[3] == "contactout_personal"
        assert personal[5] == CONFIDENCE["contactout_personal"] == 72
        assert personal[6] is False
        assert work[3] == "contactout_work"
        assert work[5] == CONFIDENCE["contactout_work"] == 80
        assert work[6] is True

    async def test_an_unknown_source_is_refused_before_it_reaches_the_constraint(self):
        """candidate_emails.source is an enumerated check constraint. Failing
        here names the problem; failing there is an opaque IntegrityError."""
        conn = StubConn()
        with pytest.raises(ValueError, match="no confidence defined"):
            await store_result(
                conn, 1, "someprovider", ProviderResult(emails=[FoundEmail("a@b.io", False)])
            )

    async def test_the_employer_domain_fills_a_gap_but_never_overwrites(self):
        conn = StubConn()
        await store_result(
            conn, 7, "contactout", ProviderResult(emails=[], employer_domain="meta.com")
        )
        [update] = [args for args in conn.executed if "employer_domain" in args[0]]
        assert "employer_domain is null" in update[0]
        assert update[1] == "meta.com"

    async def test_nothing_found_writes_nothing(self):
        conn = StubConn()
        assert await store_result(conn, 9, "contactout", ProviderResult()) == 0
        assert conn.executed == []


class TestLadderCoversTheConstraint:
    def test_every_confidence_entry_is_allowed_by_the_migration(self):
        """The ladder and the check constraint have to agree, or a write that
        passes review fails at runtime."""
        import pathlib

        sql = pathlib.Path(__file__).parent.parent / "migrations" / "007_personal_emails.sql"
        text = sql.read_text(encoding="utf-8")
        for source in CONFIDENCE:
            assert f"'{source}'" in text, f"{source} missing from the check constraint"


class TestIsConfigured:
    def test_reports_false_without_a_key(self, monkeypatch):
        from foray.config import settings as live

        monkeypatch.setattr(live, "contactout_api_key", "")
        assert ContactOutProvider().is_configured() is False

    def test_reports_true_with_one(self, monkeypatch):
        from foray.config import settings as live

        monkeypatch.setattr(live, "contactout_api_key", "k")
        assert ContactOutProvider().is_configured() is True

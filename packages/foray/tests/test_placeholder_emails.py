"""The sample-response bug, and the guards against it.

What happened: ContactOut's quota ran out and it answered **HTTP 200** with a
fully-formed fake profile -- "Example Person", a made-up employer, and the
addresses email1@example.com / email2@gmail.com. Nothing in the HTTP layer marks
that out. Thirty-two candidates were stored from it, and email2@gmail.com became
their primary_email, which is the address a sequence would have sent to.

Two independent guards, because one of them will eventually be wrong about a
payload shape nobody has seen yet:

1. the adapter refuses the whole response and *raises*, so nothing is cached;
2. store_result drops a placeholder address whatever the adapter thought.
"""

from __future__ import annotations

import asyncio

import pytest

from foray.api import internal
from foray.enrich import contactout
from foray.enrich.contactout import ContactOutProvider, _reject_if_sample
from foray.enrich.contacts import (
    CONFIDENCE,
    FoundEmail,
    ProviderResult,
    ProviderUnavailable,
    is_placeholder,
    store_result,
)

# The real payload, captured from the live API once the quota was exhausted.
SAMPLE_RESPONSE = {
    "message": (
        "This is a sample response. To unlock full access, please book a call "
        "with our sales team: https://contactout.com/meeting?utm_source=api-access"
    ),
    "status_code": 200,
    "profile": {
        "url": "https://www.linkedin.com/in/example-person",
        "email": ["email1@example.com", "email2@gmail.com"],
        "work_email": ["email1@example.com"],
        "personal_email": ["email2@gmail.com"],
        "full_name": "Example Person",
        "company": {"name": "Legros, Smitham and Kessler", "domain": "legros.com"},
    },
}

REQUESTED = "https://www.linkedin.com/in/yash-gupta-fremont"


class StubConn:
    def __init__(self) -> None:
        self.executed: list[tuple] = []

    async def execute(self, *args):
        self.executed.append(args)


class TestIsPlaceholder:
    @pytest.mark.parametrize(
        "email",
        [
            # The exact pair that reached production.
            "email1@example.com",
            "email2@gmail.com",
            # RFC 2606 / 6761 reserved domains reach nobody, by design.
            "real.looking.name@example.com",
            "someone@example.net",
            "someone@example.org",
            "someone@host.invalid",
            "someone@my.localhost",
            "someone@thing.test",
            # Documentation filler local parts on real domains -- the dangerous
            # half, because the domain accepts mail.
            "email@gmail.com",
            "test@gmail.com",
            "test1@outlook.com",
            "sample@gmail.com",
            "user2@gmail.com",
            "username@gmail.com",
            "your-email@gmail.com",
            "firstlast@gmail.com",
            "noreply@gmail.com",
            "do-not-reply@gmail.com",
            "foo@gmail.com",
            # Malformed is not contactable either.
            "",
            "no-at-sign",
        ],
    )
    def test_rejects_filler(self, email):
        assert is_placeholder(email) is True

    @pytest.mark.parametrize(
        "email",
        [
            "yash.gupta@gmail.com",
            "tara.lall@postman.com",
            "bagauntlett@mac.com",
            "pegedwards2011@gmail.com",
            "alex@streczyn.com",
            # Guarding against over-eager matching: these merely *contain* a
            # filler word, and belong to real people.
            "emailme@gmail.com",
            "testa.rossa@gmail.com",
            "samantha@gmail.com",
            "usering@gmail.com",
            "foobarbaz@gmail.com",
            "example.person@databricks.com",
            # 2026-08-20 incident: git-mined own-domain addresses a first
            # placeholder draft would have eaten. Every new gate must keep them.
            "mail@danijar.com",
            "email@jamie.sh",
            "mail@taeuk.me",
        ],
    )
    def test_keeps_real_addresses(self, email):
        assert is_placeholder(email) is False

    def test_case_and_whitespace_do_not_evade_it(self):
        assert is_placeholder("  Email1@EXAMPLE.com  ") is True


class TestRejectIfSample:
    def test_the_captured_payload_raises(self):
        with pytest.raises(ProviderUnavailable, match="out of quota"):
            _reject_if_sample(SAMPLE_RESPONSE, REQUESTED)

    def test_the_message_is_passed_through_so_the_cause_is_visible(self):
        with pytest.raises(ProviderUnavailable) as exc:
            _reject_if_sample(SAMPLE_RESPONSE, REQUESTED)
        assert "book a call" in str(exc.value)

    def test_a_sample_identity_raises_even_without_the_message(self):
        """If the vendor drops the message but keeps the fake profile, the
        payload is still about nobody."""
        payload = {"profile": dict(SAMPLE_RESPONSE["profile"])}
        with pytest.raises(ProviderUnavailable, match="not about"):
            _reject_if_sample(payload, REQUESTED)

    def test_a_real_payload_passes(self):
        payload = {
            "status_code": 200,
            "profile": {
                "full_name": "Tara Lall",
                "personal_email": ["taralall10@gmail.com"],
                "work_email": ["tara.lall@postman.com"],
            },
        }
        _reject_if_sample(payload, REQUESTED)  # no raise

    def test_an_empty_payload_is_a_miss_not_a_sample(self):
        """A genuine "we have nothing" must stay cacheable, or every miss costs
        a lookup forever."""
        _reject_if_sample({"profile": {}}, REQUESTED)  # no raise


class TestTheAdapterStoresNothingFromASample:
    def test_parse_alone_would_have_stored_nothing_anyway(self):
        """Second line of defence: even if the sample check were bypassed, every
        address in the payload is filtered as a placeholder."""
        assert ContactOutProvider()._parse(SAMPLE_RESPONSE).emails == []

    def test_the_freemail_half_is_caught_too(self):
        """email2@gmail.com is the dangerous one -- gmail.com accepts mail, so
        this address may well belong to a stranger."""
        assert contactout._usable("email2@gmail.com") is None


class TestStoreResultIsTheLastGate:
    async def test_a_placeholder_is_dropped_even_if_a_provider_returns_it(self):
        conn = StubConn()
        written = await store_result(
            conn,
            1,
            "contactout",
            ProviderResult(
                emails=[
                    FoundEmail("email2@gmail.com", is_work=False),
                    FoundEmail("email1@example.com", is_work=True),
                ]
            ),
        )
        assert written == 0
        assert [a for a in conn.executed if "insert into candidate_emails" in a[0]] == []

    async def test_a_real_address_alongside_a_placeholder_still_lands(self):
        conn = StubConn()
        written = await store_result(
            conn,
            1,
            "contactout",
            ProviderResult(
                emails=[
                    FoundEmail("email1@example.com", is_work=False),
                    FoundEmail("tara.lall@postman.com", is_work=True),
                ]
            ),
        )
        assert written == 1
        [insert] = [a for a in conn.executed if "insert into candidate_emails" in a[0]]
        assert insert[2] == "tara.lall@postman.com"
        assert insert[5] == CONFIDENCE["contactout_work"]


class TestTheLinkedInImportIsGatedToo:
    """It was the one email writer with no placeholder gate, and the one whose
    input most needs it: the addresses come from a regex over `about`,
    `bio_links` and `educations_details`, so "reach me at
    firstname.lastname@example.com" arrives shaped like a real address."""

    async def test_a_placeholder_in_an_about_section_is_not_stored(self):
        from foray.ingest.linkedin_file import store_emails

        conn = StubConn()
        stats = {"emails": 0, "placeholder_emails": 0}
        await store_emails(
            conn, 1, ["firstname.lastname@example.com", "no-reply@acme.com"], "abc123", stats
        )
        assert conn.executed == []
        assert stats == {"emails": 0, "placeholder_emails": 2}

    async def test_a_real_address_still_lands(self):
        from foray.ingest.linkedin_file import LINKEDIN_EMAIL_CONFIDENCE, store_emails

        conn = StubConn()
        stats = {"emails": 0, "placeholder_emails": 0}
        await store_emails(conn, 1, ["jane@acme.com"], "abc123", stats)
        [insert] = conn.executed
        assert insert[2] == "jane@acme.com"
        assert insert[5] == LINKEDIN_EMAIL_CONFIDENCE
        assert stats["emails"] == 1

    def test_a_regex_scrape_no_longer_outranks_every_real_source(self):
        """UPSERT_EMAIL only replaces `source` on a STRICT confidence
        improvement, so the old 80 meant a commit-mined or vendor-supplied
        address could never displace it."""
        from foray.ingest.linkedin_file import LINKEDIN_EMAIL_CONFIDENCE

        assert LINKEDIN_EMAIL_CONFIDENCE < 75  # commit
        assert CONFIDENCE["contactout_personal"] > LINKEDIN_EMAIL_CONFIDENCE
        assert LINKEDIN_EMAIL_CONFIDENCE > 25  # guess


class TestTheCacheIsNotPoisoned:
    def test_provider_unavailable_is_flagged_apart_from_an_ordinary_error(self):
        """The distinction is the whole point: an ordinary error and a quota wall
        both skip the stamp, but only one of them is worth telling the user
        about, and neither may look like "this person has no address"."""

        class OutOfQuota:
            name = "contactout"

            def is_configured(self):
                return True

            async def find_emails(self, node):
                raise ProviderUnavailable("out of quota")

        [(_, result, failure)] = asyncio.run(
            internal._look_up(OutOfQuota(), [{"login": "x", "linkedin_url": "u"}])
        )
        assert result is None
        assert failure.startswith(internal._UNAVAILABLE)

    def test_a_sample_response_never_reaches_the_stamp(self):
        """The regression, stated as directly as it can be: a payload that
        raises produces no result, and only a result stamps
        contactout_checked_at. So a quota wall cannot mark 32 candidates as
        permanently checked."""
        provider = ContactOutProvider()
        with pytest.raises(ProviderUnavailable):
            _reject_if_sample(SAMPLE_RESPONSE, REQUESTED)
        # ...and had it slipped through, there is nothing to write.
        assert provider._parse(SAMPLE_RESPONSE).emails == []

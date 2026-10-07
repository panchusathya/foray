"""The free address checks, one test per entry in address_check's failure list.

DNS is always stubbed: the suite must never touch the network (rule 8 keeps
the real resolver unreachable when the floor is off; here it is on).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import ClassVar

import dns.exception
import dns.resolver
import pytest

from juicebox.models import Enrollment, Message, utcnow
from juicebox.outreach import address_check
from juicebox.outreach.address_check import (
    RISKY,
    UNDELIVERABLE,
    hard_verdict,
    learned_risky,
    make_checker,
    receives_mail,
    soft_verdict,
)
from juicebox.outreach.sequencer import _sendable_address, enroll_candidates, run_due

# conftest stubs _lookup for every test; these drive the real one against a
# fake resolver, so keep a handle on it from before any fixture ran.
_REAL_LOOKUP = address_check._lookup


@pytest.fixture(autouse=True)
def _fresh_caches():
    address_check.reset_caches()
    yield
    address_check.reset_caches()


def _always(value):
    return lambda domain: value


class _FakeResolver:
    """Stands in for dns.resolver.Resolver; answers per (domain, type)."""

    answers: ClassVar[dict] = {}

    def __init__(self, *args, **kwargs):
        self.lifetime = None

    def resolve(self, domain, rdtype):
        outcome = self.answers.get((domain, rdtype))
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            raise dns.resolver.NoAnswer()
        return [SimpleNamespace(exchange=x) for x in outcome]


@pytest.fixture
def resolver(monkeypatch):
    _FakeResolver.answers = {}
    monkeypatch.setattr(dns.resolver, "Resolver", _FakeResolver)
    return _FakeResolver.answers


class TestDns:
    def test_our_dns_failing_is_not_evidence_about_their_domain(self, resolver):
        """Rule 1: a timeout or SERVFAIL must never hold a real person."""
        resolver[("slow.example", "MX")] = dns.exception.Timeout()
        resolver[("broken.example", "MX")] = dns.resolver.NoNameservers()
        assert _REAL_LOOKUP("slow.example") is None
        assert _REAL_LOOKUP("broken.example") is None
        assert hard_verdict("a@slow.example", lookup=_REAL_LOOKUP).score is None

    def test_a_missing_domain_cannot_receive_mail(self, resolver):
        resolver[("gone.example", "MX")] = dns.resolver.NXDOMAIN()
        assert _REAL_LOOKUP("gone.example") is False
        verdict = hard_verdict("a@gone.example", lookup=_REAL_LOOKUP)
        assert verdict.score == UNDELIVERABLE and "cannot receive" in verdict.reason

    def test_no_mx_but_an_a_record_still_receives_mail(self, resolver):
        """Rule 2: RFC 5321 implicit MX."""
        resolver[("small.example", "A")] = ["1.2.3.4"]
        assert _REAL_LOOKUP("small.example") is True

    def test_neither_mx_nor_a_is_undeliverable(self, resolver):
        assert _REAL_LOOKUP("parked.example") is False

    def test_a_null_mx_refuses_mail(self, resolver):
        """Rule 3: RFC 7505 '0 .' is an explicit no."""
        resolver[("nomail.example", "MX")] = ["."]
        assert _REAL_LOOKUP("nomail.example") is False
        resolver[("mail.example", "MX")] = ["mx1.mail.example."]
        assert _REAL_LOOKUP("mail.example") is True

    def test_one_lookup_per_domain(self):
        """Rule 10: a 500-send day must not be 500 lookups."""
        calls = []

        def lookup(domain):
            calls.append(domain)
            return True

        for _ in range(5):
            receives_mail("example.org", lookup=lookup)
        assert calls == ["example.org"]


class TestTypos:
    def test_exact_typos_are_undeliverable(self):
        verdict = hard_verdict("ada@gmial.com", lookup=_always(True))
        assert verdict.score == UNDELIVERABLE and "gmail.com" in verdict.reason

    @pytest.mark.parametrize(
        "domain", ["ymail.com", "gmail.co.uk", "googlemail.com", "yahoo.co.uk"]
    )
    def test_real_domains_are_never_typos(self, domain):
        """Rule 4: exact matches only."""
        assert hard_verdict(f"ada@{domain}", lookup=_always(True)).score is None


class TestLearned:
    def test_one_bounce_from_one_send_proves_nothing(self):
        """Rule 5: a sample, not an anecdote."""
        assert learned_risky({"tiny.example": (1, 0)}, "tiny.example") is None
        assert learned_risky({"tiny.example": (2, 0)}, "tiny.example") is None

    def test_a_high_rate_on_a_real_sample_is_held(self):
        assert learned_risky({"old.example": (6, 14)}, "old.example") == (6, 20)

    def test_a_big_domains_absolute_count_never_trips_it(self):
        assert learned_risky({"gmail.com": (8, 5632)}, "gmail.com") is None

    def test_legacy_isp_addresses_are_risky(self):
        verdict = soft_verdict("pat@aol.com", {})
        assert verdict.score == RISKY and "aol.com" in verdict.reason


class TestSendableAddress:
    def _check(self, verdicts):
        return lambda address, stored: verdicts.get(address, address_check.Verdict(stored))

    def test_a_held_first_address_falls_through_to_the_next(self):
        """Rule 9."""
        cand = SimpleNamespace(emails=["pat@aol.com", "pat@gmail.com"], raw={})
        check = self._check({"pat@aol.com": address_check.Verdict(RISKY, "old ISP address")})
        assert _sendable_address(cand, 41, check) == ("pat@gmail.com", None)

    def test_everyone_held_says_why(self):
        cand = SimpleNamespace(emails=["pat@aol.com"], raw={})
        check = self._check(
            {"pat@aol.com": address_check.Verdict(RISKY, "old ISP address (aol.com)")}
        )
        assert _sendable_address(cand, 41, check) == ("", "address_check:old ISP address (aol.com)")

    def test_floor_zero_never_calls_the_check(self):
        """Rule 8."""

        def boom(address, stored):
            raise AssertionError("checked with the floor off")

        cand = SimpleNamespace(emails=["pat@aol.com"], raw={})
        assert _sendable_address(cand, 0, boom) == ("pat@aol.com", None)


class TestCheckerAgainstHistory:
    def _sent(self, session, fixture, address, status, age):
        session.add(
            Message(
                org_id=fixture["sequence"].org_id,
                candidate_id=fixture["candidates"][0].id,
                direction="outbound",
                status=status,
                to_address=address,
                sent_at=utcnow() - age,
            )
        )
        session.flush()

    def test_a_stored_verifier_score_beats_a_guess(self, session, outreach_fixture):
        """Rule 7: 'deliverable' on aol.com sends; a typo never does."""
        check = make_checker(
            session, outreach_fixture["sequence"].org_id, utcnow(), lookup=_always(True)
        )
        assert check("pat@aol.com", 90).score == 90
        assert check("pat@gmial.com", 90).score == UNDELIVERABLE

    def test_an_address_that_already_delivered_is_proven(self, session, outreach_fixture):
        """Rule 6: step 1 landed, so step 2 is not held by a guess."""
        org = outreach_fixture["sequence"].org_id
        self._sent(session, outreach_fixture, "pat@aol.com", "sent", timedelta(days=3))
        check = make_checker(session, org, utcnow(), lookup=_always(True))
        assert check("pat@aol.com", None).score is None
        # Sent minutes ago: a bounce could still be on its way.
        self._sent(session, outreach_fixture, "sam@aol.com", "sent", timedelta(minutes=5))
        assert check("sam@aol.com", None).score == RISKY

    def test_a_bounced_address_is_never_proven(self, session, outreach_fixture):
        org = outreach_fixture["sequence"].org_id
        self._sent(session, outreach_fixture, "pat@aol.com", "sent", timedelta(days=3))
        self._sent(session, outreach_fixture, "pat@aol.com", "bounced", timedelta(days=2))
        check = make_checker(session, org, utcnow(), lookup=_always(True))
        assert check("pat@aol.com", None).score == RISKY

    def test_the_org_history_teaches_a_domain(self, session, outreach_fixture):
        org = outreach_fixture["sequence"].org_id
        for i in range(3):
            self._sent(
                session, outreach_fixture, f"b{i}@stale.example", "bounced", timedelta(days=2)
            )
        for i in range(5):
            self._sent(session, outreach_fixture, f"d{i}@stale.example", "sent", timedelta(days=2))
        check = make_checker(session, org, utcnow(), lookup=_always(True))
        verdict = check("new@stale.example", None)
        assert verdict.score == RISKY and "bounced 3 of 8" in verdict.reason


class TestEndToEndSend:
    def test_a_held_address_stops_the_enrolment_and_says_why(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("FORAY_MIN_REACH_SCORE", "41")
        monkeypatch.setattr(address_check, "_lookup", _always(True))
        reset_settings_cache()
        try:
            ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
            ada.emails = ["ada@aol.com"]
            ada.raw = {}
            session.commit()
            enroll_candidates(
                session, sequence=outreach_fixture["sequence"], candidate_ids=[ada.id]
            )
            session.commit()
            summary = run_due(session, org_id=outreach_fixture["sequence"].org_id)
            session.commit()

            assert summary.failed == 1 and not mailbox.outbox
            enrollment = session.query(Enrollment).filter_by(candidate_id=ada.id).one()
            assert enrollment.status == "stopped"
            assert enrollment.stopped_reason == "address_check"
            message = session.query(Message).filter_by(enrollment_id=enrollment.id).one()
            assert message.error == "old ISP address (aol.com)"
        finally:
            monkeypatch.delenv("FORAY_MIN_REACH_SCORE")
            reset_settings_cache()

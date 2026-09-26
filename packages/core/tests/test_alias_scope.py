"""Aliases moved from per-candidate to per-application.

The reason is attribution: a candidate-scoped alias says who a reply is about
and leaves *which pursuit* to a sender-domain heuristic that correctly declines
when two applications sit behind one ATS vendor's domain.

The risk is the old rows. Employers are already writing to candidate-scoped
addresses, and an alias that stopped resolving would drop real mail on the
floor silently. So the tests that matter here are the legacy ones.
"""

from __future__ import annotations

import pytest

from juicebox.applications import alias
from juicebox.models import Application, Candidate, JobPosting
from juicebox.repository import upsert_candidates
from juicebox.security import alias_token, application_alias_token


@pytest.fixture
def aliased(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_ALIAS_ADDRESS", "anna@goforay.io")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    row = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
    row.emails = ["ada@example.com"]
    session.flush()
    return row


def _application(session, account, candidate, source_id="p1") -> Application:
    posting = JobPosting(
        org_id=account["org_id"],
        company="Acme",
        title="Analyst",
        source="test",
        source_id=source_id,
    )
    session.add(posting)
    session.flush()
    row = Application(
        org_id=account["org_id"],
        candidate_id=candidate.id,
        job_posting_id=posting.id,
        status="submitted",
    )
    session.add(row)
    session.flush()
    return row


class TestTokens:
    def test_the_two_scopes_never_collide(self):
        """Same id, same length, same position in the address -- and they must
        still not be interchangeable, which is what the purpose string buys."""
        assert alias_token("x1") != application_alias_token("x1")

    def test_it_is_lowercase_and_alphanumeric(self):
        """Local parts survive case changes in transit; a token that stopped
        matching after a relay lowercased it would be unroutable."""
        token = application_alias_token("app_1")
        assert token.isalnum() and token.islower()


class TestMinting:

    def test_minting_twice_reuses_the_address(self, session, account, candidate, aliased):
        """Reissuing would change an address the employer is already using."""
        application = _application(session, account, candidate)
        first = alias.ensure_for_application(session, application)
        second = alias.ensure_for_application(session, application)
        assert first.id == second.id

    def test_a_legacy_candidate_alias_does_not_absorb_new_applications(
        self, session, account, candidate, aliased
    ):
        """The bug this replaced: falling back to the candidate's old alias
        meant no existing candidate ever got a per-application address, so the
        feature applied to nobody. Caught by a dry run in production."""
        legacy = alias.ensure(session, candidate)
        application = _application(session, account, candidate)
        minted = alias.ensure_for_application(session, application)

        assert minted.id != legacy.id
        assert minted.scope == "application"
        assert minted.application_id == application.id

    def test_the_legacy_alias_still_resolves_afterwards(
        self, session, account, candidate, aliased
    ):
        """Not orphaning it is the whole reason the fallback looked right."""
        legacy = alias.ensure(session, candidate)
        application = _application(session, account, candidate)
        alias.ensure_for_application(session, application)

        found, application_id = alias.resolve(session, legacy.address)
        assert found.id == candidate.id
        assert application_id == ""


class TestResolving:
    def test_an_application_alias_resolves_to_both(self, session, account, candidate, aliased):
        application = _application(session, account, candidate)
        row = alias.ensure_for_application(session, application)
        found, application_id = alias.resolve(session, row.address)
        assert found.id == candidate.id
        assert application_id == application.id

    def test_a_legacy_candidate_alias_still_resolves(self, session, account, candidate, aliased):
        """The one that must not break. Real employers are writing here."""
        legacy = alias.ensure(session, candidate)
        found, application_id = alias.resolve(session, legacy.address)
        assert found.id == candidate.id
        assert application_id == "", "a candidate alias cannot name a pursuit"

    def test_a_forged_token_is_refused(self, session, account, candidate, aliased):
        """The row lookup alone would make any stored alias addressable by
        anyone who learned it, so the HMAC is verified against the row."""
        application = _application(session, account, candidate)
        row = alias.ensure_for_application(session, application)
        row.local_part = "deadbeefdeadbeef"
        row.address = "anna+deadbeefdeadbeef@goforay.io"
        session.flush()
        found, _ = alias.resolve(session, row.address)
        assert found is None

    def test_a_scope_that_disagrees_with_its_token_is_refused(
        self, session, account, candidate, aliased
    ):
        """A row claiming application scope whose token is a candidate token."""
        legacy = alias.ensure(session, candidate)
        application = _application(session, account, candidate)
        legacy.scope = "application"
        legacy.application_id = application.id
        session.flush()
        found, _ = alias.resolve(session, legacy.address)
        assert found is None

    def test_a_revoked_alias_stops_resolving(self, session, account, candidate, aliased):
        application = _application(session, account, candidate)
        row = alias.ensure_for_application(session, application)
        row.status = "revoked"
        session.flush()
        assert alias.resolve(session, row.address)[0] is None

    def test_candidate_for_resolves_an_application_alias(
        self, session, account, candidate, aliased
    ):
        """It used to verify every row against the candidate HMAC, so a
        per-application alias — the only kind we mint now — returned None."""
        application = _application(session, account, candidate)
        row = alias.ensure_for_application(session, application)
        assert alias.candidate_for(session, row.address).id == candidate.id

    def test_revoke_covers_every_alias_for_the_candidate(
        self, session, account, candidate, aliased
    ):
        legacy = alias.ensure(session, candidate)
        application = _application(session, account, candidate)
        app_row = alias.ensure_for_application(session, application)
        alias.revoke(session, candidate.id)
        assert legacy.status == "revoked"
        assert app_row.status == "revoked"

    def test_the_bare_mailbox_resolves_to_nobody(self, session, aliased):
        """The bug that started this: a reply to anna@ has no token, so it is
        attributable to no one."""
        assert alias.resolve(session, "anna@goforay.io") == (None, "")


class TestSendingIdentity:
    def test_an_application_send_uses_the_application_alias(
        self, session, account, candidate, aliased
    ):
        application = _application(session, account, candidate)
        from_address, reply_to = alias.sending_identity(session, candidate, application)
        assert from_address == f"anna+{application_alias_token(application.id)}@goforay.io"
        assert reply_to == "ada@example.com"

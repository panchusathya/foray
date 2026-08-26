"""The employer inbox: storing what was said, and showing it to one candidate.

Two things are load-bearing here and neither is the happy path.

The first is idempotency. A sync page is replayed whenever the worker dies
holding one, and before these rows existed a replay would click a verification
link twice and re-move a status a human may already have corrected.

The second is the wall. This is candidate-facing data on the portal origin, so
every query filters org AND candidate in the WHERE clause. ``TestTheWall`` is
what keeps that from being refactored into a load-then-check.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.applications.inbox import (
    KIND_ACKNOWLEDGED,
    KIND_ASSESSMENT,
    KIND_INTERVIEW,
    KIND_OTHER,
    KIND_REJECTED,
    KIND_VERIFICATION,
    classify,
    handle,
)
from juicebox.models import (
    Application,
    ApplicationMessage,
    ApplicationSubmission,
    Candidate,
    JobPosting,
    utcnow,
)
from juicebox.portal.links import mint_session_token
from juicebox.repository import upsert_candidates


@pytest.fixture
def candidate(session, account, sample_candidates):
    upsert_candidates(session, org_id=account["org_id"], payloads=sample_candidates[:1])
    session.commit()
    row = session.query(Candidate).filter_by(full_name="Ada Okonkwo").one()
    row.emails = ["ada@example.com"]
    session.flush()
    return row


@pytest.fixture
def aliased(monkeypatch):
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("APPLY_ALIAS_ADDRESS", "anna@goforay.io")
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def no_relay(monkeypatch):
    """The relay needs a sender; these tests are about the stored row."""
    from juicebox.applications import inbox as inbox_mod

    monkeypatch.setattr(inbox_mod, "relay", lambda *a, **k: False)


def _alias_for(session, candidate) -> str:
    """Issue the alias, as the submission path does on first use."""
    from juicebox.applications import alias

    row = alias.ensure(session, candidate)
    return row.address


def _headers(account, candidate) -> dict:
    token = mint_session_token(org_id=account["org_id"], candidate_id=candidate.id)
    return {"Authorization": f"Bearer {token}"}


class TestClassify:
    """The two new labels, and the ordering that makes them useful."""

    def test_an_interview_invitation(self):
        assert classify("Next steps", "We'd like to schedule an interview").kind == KIND_INTERVIEW

    def test_an_assessment(self):
        assert classify("Your next step", "Please complete the assessment").kind == KIND_ASSESSMENT

    def test_interview_beats_the_acknowledgement_it_opens_with(self):
        """Almost every invitation opens by thanking you for applying."""
        body = "Thank you for applying! We would like to speak with you about the role."
        assert classify("", body).kind == KIND_INTERVIEW

    def test_rejection_still_beats_interview_wording(self):
        """A rejection naming the next round is still a rejection."""
        body = "Thank you for applying. We will not be moving forward to the next round."
        assert classify("", body).kind == KIND_REJECTED

    def test_a_plain_acknowledgement_is_unchanged(self):
        assert classify("", "We have received your application").kind == KIND_ACKNOWLEDGED

    def test_an_ordinary_note_is_not_labelled(self):
        assert classify("Quick question", "Are you still in London?").kind == KIND_OTHER


class TestStoring:
    def test_a_message_is_kept_with_its_body(self, session, account, candidate, aliased, no_relay):
        out = handle(
            session,
            org_id=account["org_id"],
            to_addresses=(_alias_for(session, candidate),),
            from_address="Greenhouse Recruiting <no-reply@greenhouse.io>",
            subject="Thank you for applying to Acme",
            body="We have received your application and will be in touch.",
            native_id="zm-1",
        )
        assert out["routed"] is True

        row = session.scalars(select(ApplicationMessage)).one()
        assert row.candidate_id == candidate.id
        assert row.kind == KIND_ACKNOWLEDGED
        assert row.from_address == "no-reply@greenhouse.io"
        assert row.from_name == "Greenhouse Recruiting"
        assert row.body.startswith("We have received")
        assert row.provider_native_id == "zm-1"
        assert row.read_at is None

    def test_a_replayed_page_does_not_store_or_act_twice(
        self, session, account, candidate, aliased, no_relay
    ):
        payload = dict(
            org_id=account["org_id"],
            to_addresses=(_alias_for(session, candidate),),
            from_address="no-reply@greenhouse.io",
            subject="Verify",
            body="Confirm here: https://boards.greenhouse.io/verify?t=abc",
            native_id="zm-dup",
        )
        first = handle(session, **payload)
        second = handle(session, **payload)

        assert first["kind"] == KIND_VERIFICATION
        assert second["reason"] == "already handled"
        # Still routed, or the caller falls through to the recruiter-reply path.
        assert second["routed"] is True
        assert session.scalars(select(ApplicationMessage)).all().__len__() == 1

    def test_mail_with_no_matching_application_is_still_kept(
        self, session, account, candidate, aliased, no_relay
    ):
        """Attribution to a candidate is certain; to an application it is a guess."""
        handle(
            session,
            org_id=account["org_id"],
            to_addresses=(_alias_for(session, candidate),),
            from_address="someone@unknown-employer.com",
            subject="Hello",
            body="Are you still interested?",
            native_id="zm-2",
        )
        row = session.scalars(select(ApplicationMessage)).one()
        assert row.application_id is None
        assert row.kind == KIND_OTHER

    def test_mail_to_a_stranger_alias_is_not_stored(
        self, session, account, candidate, aliased, no_relay
    ):
        """The address is ours, so the caller must not fall through to the
        recruiter-reply path. Nothing is stored because the token does not
        resolve to anyone."""
        out = handle(
            session,
            org_id=account["org_id"],
            to_addresses=("anna+notatoken@goforay.io",),
            from_address="x@y.com",
            subject="s",
            body="b",
            native_id="zm-3",
        )
        assert out["routed"] is True
        assert out["reason"] == "unresolved alias"
        assert session.scalars(select(ApplicationMessage)).all() == []


def _message(session, account, candidate, **kwargs) -> ApplicationMessage:
    row = ApplicationMessage(
        org_id=kwargs.pop("org_id", account["org_id"]),
        candidate_id=kwargs.pop("candidate_id", candidate.id),
        kind=kwargs.pop("kind", KIND_OTHER),
        subject=kwargs.pop("subject", "Subject"),
        body=kwargs.pop("body", "Body text"),
        from_address=kwargs.pop("from_address", "recruiter@acme.com"),
        received_at=utcnow(),
        **kwargs,
    )
    session.add(row)
    session.flush()
    return row


class TestTrackerApi:
    def test_lists_newest_first_with_counts(self, client, session, account, candidate):
        _message(session, account, candidate, kind=KIND_INTERVIEW, subject="Interview")
        _message(session, account, candidate, kind=KIND_REJECTED, subject="No")
        _message(session, account, candidate, kind=KIND_REJECTED, subject="Also no")
        session.commit()

        body = client.get(
            "/v1/portal/tracker/messages", headers=_headers(account, candidate)
        ).json()
        assert body["counts"]["all"] == 3
        assert body["counts"][KIND_REJECTED] == 2
        assert body["unread"] == 3
        assert len(body["messages"]) == 3
        assert KIND_INTERVIEW in body["kinds"]

    def test_filters_by_kind_without_changing_the_counts(
        self, client, session, account, candidate
    ):
        """The chips describe the inbox, not the current filter."""
        _message(session, account, candidate, kind=KIND_INTERVIEW)
        _message(session, account, candidate, kind=KIND_REJECTED)
        session.commit()

        body = client.get(
            f"/v1/portal/tracker/messages?kind={KIND_INTERVIEW}",
            headers=_headers(account, candidate),
        ).json()
        assert len(body["messages"]) == 1
        assert body["counts"]["all"] == 2

    def test_the_list_carries_a_preview_not_the_body(self, client, session, account, candidate):
        _message(session, account, candidate, body="x" * 500)
        session.commit()
        body = client.get(
            "/v1/portal/tracker/messages", headers=_headers(account, candidate)
        ).json()
        assert "body" not in body["messages"][0]
        assert len(body["messages"][0]["preview"]) <= 180

    def test_opening_one_returns_the_body_and_marks_it_read(
        self, client, session, account, candidate
    ):
        row = _message(session, account, candidate, body="The full text")
        session.commit()

        body = client.get(
            f"/v1/portal/tracker/messages/{row.id}", headers=_headers(account, candidate)
        ).json()
        assert body["body"] == "The full text"
        assert body["read"] is True, "opening it is what marks it read"

        again = client.get(
            "/v1/portal/tracker/messages", headers=_headers(account, candidate)
        ).json()
        assert again["unread"] == 0

    def test_pipeline_shows_live_applications_only(
        self, client, session, account, candidate
    ):
        # One application per (candidate, posting), so two postings.
        for index, status_value in enumerate(("submitted", "withdrawn")):
            posting = JobPosting(
                org_id=account["org_id"],
                company="Acme",
                title="Analyst",
                source="test",
                source_id=f"acme-analyst-{index}",
            )
            session.add(posting)
            session.flush()
            session.add(
                Application(
                    org_id=account["org_id"],
                    candidate_id=candidate.id,
                    job_posting_id=posting.id,
                    status=status_value,
                )
            )
        session.commit()

        body = client.get(
            "/v1/portal/tracker/pipeline", headers=_headers(account, candidate)
        ).json()
        assert len(body["applications"]) == 1
        assert body["applications"][0]["status"] == "submitted"
        # No attempt row, so nothing claims it really went.
        assert body["applications"][0]["was_really_sent"] is False

    def test_a_memory_dry_run_is_not_really_sent(
        self, client, session, account, candidate
    ):
        posting = JobPosting(
            org_id=account["org_id"],
            company="Acme",
            title="Analyst",
            source="test",
            source_id="acme-memory-dry",
        )
        session.add(posting)
        session.flush()
        application = Application(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            job_posting_id=posting.id,
            status="submitted",
            submitted_at=utcnow(),
        )
        session.add(application)
        session.flush()
        session.add(
            ApplicationSubmission(
                org_id=account["org_id"],
                application_id=application.id,
                method="memory",
                status="succeeded",
            )
        )
        session.commit()

        body = client.get(
            "/v1/portal/tracker/pipeline", headers=_headers(account, candidate)
        ).json()
        assert body["applications"][0]["was_really_sent"] is False


class TestTheWall:
    """Candidate-facing data on the candidate origin. Filter, never load-then-check."""

    def test_another_candidates_mail_is_invisible(self, client, session, account, candidate):
        other = Candidate(
            org_id=account["org_id"],
            full_name="Someone Else",
            source="manual",
            source_id="other-1",
        )
        session.add(other)
        session.flush()
        _message(session, account, candidate, candidate_id=other.id, subject="Not yours")
        session.commit()

        body = client.get(
            "/v1/portal/tracker/messages", headers=_headers(account, candidate)
        ).json()
        assert body["messages"] == []
        assert body["counts"].get("all", 0) == 0

    def test_another_candidates_message_cannot_be_opened_by_id(
        self, client, session, account, candidate
    ):
        other = Candidate(
            org_id=account["org_id"],
            full_name="Someone Else",
            source="manual",
            source_id="other-1",
        )
        session.add(other)
        session.flush()
        row = _message(session, account, candidate, candidate_id=other.id)
        session.commit()

        response = client.get(
            f"/v1/portal/tracker/messages/{row.id}", headers=_headers(account, candidate)
        )
        assert response.status_code == 404

    def test_no_session_is_a_401(self, client, session, account, candidate):
        assert client.get("/v1/portal/tracker/messages").status_code == 401


class TestRepliableAddress:
    """ATS mail is sent from a robot; the address that routes back is Reply-To."""

    def _msg(self, **kwargs):
        return ApplicationMessage(org_id="o", candidate_id="c", **kwargs)

    def test_reply_to_beats_the_robot_it_came_from(self):
        from juicebox.applications.inbox import repliable_address

        row = self._msg(from_address="no-reply@ashbyhq.com", reply_to_address="dana@parasail.ai")
        assert repliable_address(row) == "dana@parasail.ai"

    def test_from_is_used_when_a_human_wrote_directly(self):
        from juicebox.applications.inbox import repliable_address

        assert repliable_address(self._msg(from_address="dana@parasail.ai")) == "dana@parasail.ai"

    def test_a_no_reply_everywhere_means_nobody(self):
        """Workday verification mail. A reply box here would just bounce."""
        from juicebox.applications.inbox import repliable_address

        row = self._msg(from_address="no-reply@myworkday.com", reply_to_address="noreply@acme.com")
        assert repliable_address(row) == ""

    def test_it_unwraps_a_display_name(self):
        from juicebox.applications.inbox import repliable_address

        row = self._msg(from_address="Dana Whitfield <dana@parasail.ai>")
        assert repliable_address(row) == "dana@parasail.ai"

    def test_a_noreply_domain_with_a_human_local_part_is_fine(self):
        """The marker is on the local part; noreply.co is a real domain."""
        from juicebox.applications.inbox import repliable_address

        assert repliable_address(self._msg(from_address="dana@noreply.co")) == "dana@noreply.co"


class TestReplying:
    def _seed(self, session, account, candidate, **kwargs):
        row = _message(
            session,
            account,
            candidate,
            alias_address="anna+tok@goforay.io",
            reply_to_address=kwargs.pop("reply_to_address", "dana@parasail.ai"),
            rfc_message_id=kwargs.pop("rfc_message_id", "<orig@ashbyhq.com>"),
            subject=kwargs.pop("subject", "Next steps"),
            **kwargs,
        )
        session.commit()
        return row

    def test_a_reply_goes_out_as_the_alias(self, client, session, account, candidate, monkeypatch):
        from juicebox.outreach import sender as sender_mod
        from juicebox.routers import portal as portal_mod

        outbox = sender_mod.MemorySender()
        monkeypatch.setattr(portal_mod, "sender_for_org", lambda db, org_id: outbox)

        row = self._seed(session, account, candidate)
        response = client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "Tuesday works."},
        )
        assert response.status_code == 201

        sent = outbox.outbox[-1]
        assert sent.to == "dana@parasail.ai"
        # Never the candidate's own address: the employer has no record of it.
        assert sent.from_email == "anna+tok@goforay.io"
        assert sent.headers["Reply-To"] == "anna+tok@goforay.io"
        assert sent.in_reply_to == "<orig@ashbyhq.com>"
        assert sent.subject == "Re: Next steps"

    def test_it_does_not_double_the_re_prefix(self, client, session, account, candidate, monkeypatch):
        from juicebox.outreach import sender as sender_mod
        from juicebox.routers import portal as portal_mod

        outbox = sender_mod.MemorySender()
        monkeypatch.setattr(portal_mod, "sender_for_org", lambda db, org_id: outbox)

        row = self._seed(session, account, candidate, subject="Re: Next steps")
        client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "ok"},
        )
        assert outbox.outbox[-1].subject == "Re: Next steps"

    def test_the_reply_is_stored_as_part_of_the_thread(
        self, client, session, account, candidate, monkeypatch
    ):
        from juicebox.outreach import sender as sender_mod
        from juicebox.routers import portal as portal_mod

        monkeypatch.setattr(
            portal_mod, "sender_for_org", lambda db, org_id: sender_mod.MemorySender()
        )
        row = self._seed(session, account, candidate)
        client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "Tuesday works."},
        )
        stored = session.scalars(
            select(ApplicationMessage).where(ApplicationMessage.direction == "outbound")
        ).all()
        assert len(stored) == 1
        assert stored[0].body == "Tuesday works."
        assert stored[0].read_at is not None, "your own reply is not unread mail"

    def test_a_no_reply_message_refuses(self, client, session, account, candidate):
        row = self._seed(
            session,
            account,
            candidate,
            from_address="no-reply@myworkday.com",
            reply_to_address="",
        )
        response = client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "hello"},
        )
        assert response.status_code == 409
        assert "no-reply" in response.json()["detail"]

    def test_an_empty_body_refuses(self, client, session, account, candidate):
        row = self._seed(session, account, candidate)
        response = client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "   "},
        )
        assert response.status_code == 422

    def test_a_send_failure_is_not_reported_as_sent(
        self, client, session, account, candidate, monkeypatch
    ):
        from juicebox.outreach import sender as sender_mod
        from juicebox.routers import portal as portal_mod

        monkeypatch.setattr(
            portal_mod,
            "sender_for_org",
            lambda db, org_id: sender_mod.FailingSender("resend 403"),
        )
        row = self._seed(session, account, candidate)
        response = client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "hello"},
        )
        assert response.status_code == 502
        assert session.scalars(
            select(ApplicationMessage).where(ApplicationMessage.direction == "outbound")
        ).all() == []

    def test_another_candidates_message_cannot_be_replied_to(
        self, client, session, account, candidate
    ):
        other = Candidate(
            org_id=account["org_id"],
            full_name="Someone Else",
            source="manual",
            source_id="other-reply",
        )
        session.add(other)
        session.flush()
        row = _message(session, account, candidate, candidate_id=other.id)
        session.commit()
        response = client.post(
            f"/v1/portal/tracker/messages/{row.id}/reply",
            headers=_headers(account, candidate),
            json={"body": "hello"},
        )
        assert response.status_code == 404

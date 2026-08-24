"""The "call me" flow: dialling a candidate who asked to be called.

This endpoint turns one HTTP request into a ringing phone at a number the caller
chooses. That makes it a harassment tool and a metered one, so the guards are the
feature and most of what follows tests a refusal rather than a success.

It is also the one intake route that is *authenticated*. The other two are open
because a phone call carries no principal and a provider signs its requests
instead; this one originates with us, so there is nothing to verify and the
marketing site's backend calls it with an API key.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.intake.providers import FakeVoiceProvider, set_voice_provider
from juicebox.models import Candidate, CandidatePhone, IntakeCall, IntakeNumber, Suppression

OUR_NUMBER = "+16283865454"
TARGET = "+14155550123"


@pytest.fixture
def fake_voice():
    provider = FakeVoiceProvider()
    set_voice_provider(provider)
    yield provider
    set_voice_provider(None)


@pytest.fixture
def owned_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"],
        provider="retell",
        phone=OUR_NUMBER,
        outbound_agent_id="agent_outbound_1",
    )
    session.add(row)
    session.commit()
    return row


def call_me(client, account, **body):
    payload = {"phone": TARGET, "name": "Ada Okonkwo", "email": "ada@example.com"}
    payload.update(body)
    return client.post("/v1/intake/call-me", json=payload, headers=account["headers"])


class TestAuthentication:
    def test_an_anonymous_request_is_refused(self, client, owned_number, fake_voice):
        response = client.post("/v1/intake/call-me", json={"phone": TARGET})
        assert response.status_code == 401
        assert fake_voice.placed == []

    def test_a_bad_token_is_refused(self, client, owned_number, fake_voice):
        response = client.post(
            "/v1/intake/call-me",
            json={"phone": TARGET},
            headers={"Authorization": "Bearer nonsense"},
        )
        assert response.status_code == 401
        assert fake_voice.placed == []


class TestPlacingTheCall:
    def test_it_dials_from_our_number_with_the_outbound_agent(
        self, client, session, account, owned_number, fake_voice
    ):
        response = call_me(client, account)
        assert response.status_code == 202, response.text

        assert len(fake_voice.placed) == 1
        placed = fake_voice.placed[0]
        assert placed["from_number"] == OUR_NUMBER
        assert placed["to_number"] == TARGET
        # Per-call override rather than attaching the agent to the number:
        # attaching is what makes a bot answer real inbound callers.
        assert placed["agent_id"] == "agent_outbound_1"

    def test_the_call_row_exists_before_any_webhook_arrives(
        self, client, session, account, owned_number, fake_voice
    ):
        """The reason an outbound or web call can resolve its org at all: there
        is no dialled number to look up, so the trusted source is a row we own."""
        body = call_me(client, account).json()
        call = session.scalars(select(IntakeCall)).one()
        assert call.id == body["intake_call_id"]
        assert call.org_id == account["org_id"]
        assert call.entry_point == "outbound"
        assert call.status == "in_progress"
        assert call.provider_call_id == "fake_call_1"

    def test_the_requester_becomes_a_candidate(
        self, client, session, account, owned_number, fake_voice
    ):
        """Creating a person here is right, and it is the opposite of the rule
        for a cold inbound call: they filled in a form asking to be phoned."""
        call_me(client, account)
        candidate = session.scalars(select(Candidate)).one()
        assert candidate.full_name == "Ada Okonkwo"
        assert candidate.first_name == "Ada"
        assert TARGET in candidate.phones
        assert "ada@example.com" in candidate.emails
        assert candidate.crm_tracked is True
        # Projected, so the inbound line can recognise them when they call back.
        assert session.scalar(select(CandidatePhone.phone)) == TARGET

    def test_a_known_number_is_reused_not_duplicated(
        self, client, session, account, owned_number, fake_voice
    ):
        from juicebox.repository import sync_candidate_phones

        existing = Candidate(
            org_id=account["org_id"],
            source="test",
            source_id="ada",
            full_name="Ada O",
            phones=[TARGET],
        )
        session.add(existing)
        session.flush()
        sync_candidate_phones(session, existing)
        session.commit()

        call_me(client, account)
        assert len(session.scalars(select(Candidate)).all()) == 1
        session.refresh(existing)
        # The form's email is added; the name we already had is not overwritten.
        assert "ada@example.com" in existing.emails
        assert existing.full_name == "Ada O"

    def test_a_known_email_is_reused_when_the_number_is_new(
        self, client, session, account, owned_number, fake_voice
    ):
        from juicebox.repository import sync_candidate_emails

        existing = Candidate(
            org_id=account["org_id"],
            source="test",
            source_id="ada",
            full_name="Ada O",
            emails=["ada@example.com"],
        )
        session.add(existing)
        session.flush()
        sync_candidate_emails(session, existing)
        session.commit()

        call_me(client, account)
        assert len(session.scalars(select(Candidate)).all()) == 1
        session.refresh(existing)
        assert TARGET in existing.phones


class TestRefusals:
    def test_an_unnormalisable_number_is_refused(
        self, client, account, owned_number, fake_voice
    ):
        response = call_me(client, account, phone="555-0123")
        assert response.status_code == 400
        assert "country code" in response.json()["detail"]
        assert fake_voice.placed == []

    def test_our_own_number_is_refused(self, client, account, owned_number, fake_voice):
        """Dialling ourselves connects the agent to the agent."""
        response = call_me(client, account, phone=OUR_NUMBER)
        assert response.status_code == 400
        assert fake_voice.placed == []

    def test_no_configured_number_is_refused(self, client, account, fake_voice):
        response = call_me(client, account)
        assert response.status_code == 400
        assert "No intake number" in response.json()["detail"]
        assert fake_voice.placed == []

    def test_a_deactivated_number_cannot_dial(
        self, client, session, account, owned_number, fake_voice
    ):
        owned_number.active = False
        session.commit()
        assert call_me(client, account).status_code == 400
        assert fake_voice.placed == []

    def test_a_do_not_contact_candidate_is_never_dialled(
        self, client, session, account, owned_number, fake_voice
    ):
        session.add(
            Suppression(org_id=account["org_id"], email="ada@example.com", reason="manual")
        )
        session.commit()
        response = call_me(client, account)
        assert response.status_code == 400
        assert "do-not-contact" in response.json()["detail"]
        assert fake_voice.placed == []

    def test_the_same_number_is_not_redialled_immediately(
        self, client, session, account, owned_number, fake_voice
    ):
        """Without a cooldown a loop pointed at a stranger's number costs us per
        minute and costs them their afternoon."""
        assert call_me(client, account).status_code == 202
        second = call_me(client, account)
        assert second.status_code == 400
        assert "recently" in second.json()["detail"]
        assert len(fake_voice.placed) == 1

    def test_the_cooldown_expires(
        self, client, session, account, owned_number, fake_voice
    ):
        from datetime import timedelta

        from juicebox.models import utcnow

        assert call_me(client, account).status_code == 202
        call = session.scalars(select(IntakeCall)).one()
        call.created_at = utcnow() - timedelta(hours=2)
        session.commit()

        assert call_me(client, account).status_code == 202
        assert len(fake_voice.placed) == 2

    def test_a_provider_failure_leaves_no_call_row(
        self, client, session, account, owned_number
    ):
        """A row for a call that never happened would sit in the review queue
        forever waiting for a transcript that is not coming."""
        provider = FakeVoiceProvider(dial_error="number not owned")
        set_voice_provider(provider)
        try:
            response = call_me(client, account)
        finally:
            set_voice_provider(None)

        assert response.status_code == 400
        assert "could not place the call" in response.json()["detail"]
        assert session.scalars(select(IntakeCall)).all() == []

    def test_the_provider_error_is_not_leaked_to_the_caller(
        self, client, session, account, owned_number
    ):
        """"number not owned" is an operator's problem, not a website visitor's."""
        set_voice_provider(FakeVoiceProvider(dial_error="number not owned"))
        try:
            detail = call_me(client, account).json()["detail"]
        finally:
            set_voice_provider(None)
        assert "not owned" not in detail

    def test_another_orgs_number_cannot_be_used(
        self, client, session, account, fake_voice
    ):
        """The number is looked up by the caller's own org, never chosen."""
        from juicebox.models import Org

        other = Org(name="Other Firm", slug="other-firm")
        session.add(other)
        session.flush()
        session.add(IntakeNumber(org_id=other.id, provider="retell", phone=OUR_NUMBER))
        session.commit()

        response = call_me(client, account)
        assert response.status_code == 400
        assert "No intake number" in response.json()["detail"]
        assert fake_voice.placed == []

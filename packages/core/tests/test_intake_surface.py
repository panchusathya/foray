"""Authed intake read path, provenance, phone suppression, rematch."""

from __future__ import annotations

from juicebox.intake.service import extract_call
from juicebox.models import (
    Candidate,
    IntakeCall,
    Org,
)
from juicebox.outreach.suppressions import is_suppressed, suppress
from juicebox.repository import sync_candidate_phones

TRANSCRIPT = (
    "Agent: This call is recorded, is that OK?\n"
    "Candidate: Yes.\n"
    "Agent: What are you looking for?\n"
    "Candidate: A senior machine learning engineer role in San Francisco.\n"
    "Candidate: I'd want four hundred k total, and I can start in two months.\n"
)


def _person(session, org_id, *, name="Ada Okonkwo", phones=None, source_id="c1"):
    person = Candidate(
        org_id=org_id,
        source="test",
        source_id=source_id,
        full_name=name,
        phones=phones or ["+14155550123"],
    )
    session.add(person)
    session.flush()
    sync_candidate_phones(session, person)
    session.commit()
    return person


def _call(session, org_id, *, candidate_id=None, provider_call_id="prov-1", **extra):
    fields = {
        "org_id": org_id,
        "candidate_id": candidate_id,
        "provider": "fake",
        "provider_call_id": provider_call_id,
        "entry_point": "inbound",
        "from_number": "+14155550123",
        "to_number": "+16283865454",
        "status": "completed",
        "transcript": TRANSCRIPT,
    }
    fields.update(extra)
    row = IntakeCall(**fields)
    session.add(row)
    session.commit()
    return row


def _other_headers(client):
    other = client.post(
        "/v1/auth/signup",
        json={
            "org_name": "Rival",
            "email": "rival@rival-talent.com",
            "password": "supersecret1",
            "name": "Bo",
        },
    )
    assert other.status_code == 201, other.text
    return {"Authorization": f"Bearer {other.json()['access_token']}"}


class TestReadApi:
    def test_lists_and_gets_are_org_scoped(self, client, session, account):
        person = _person(session, account["org_id"])
        call = _call(session, account["org_id"], candidate_id=person.id)
        extract_call(session, call)
        session.commit()

        other = Org(name="Other Firm", slug="other-firm")
        session.add(other)
        session.flush()
        foreign = _call(
            session,
            other.id,
            provider_call_id="foreign-1",
            from_number="+14155559999",
        )
        session.commit()

        mine = client.get("/v1/intake/calls", headers=account["headers"]).json()
        assert [row["id"] for row in mine] == [call.id]
        assert client.get(f"/v1/intake/calls/{call.id}", headers=account["headers"]).status_code == 200

        rival = _other_headers(client)
        assert client.get("/v1/intake/calls", headers=rival).json() == []
        assert client.get(f"/v1/intake/calls/{call.id}", headers=rival).status_code == 404
        assert client.get(f"/v1/intake/calls/{foreign.id}", headers=account["headers"]).status_code == 404
        assert (
            client.get(
                f"/v1/intake/preferences/{person.id}", headers=rival
            ).status_code
            == 404
        )

    def test_another_org_cannot_attribute_your_call(self, client, session, account):
        call = _call(session, account["org_id"])
        session.commit()
        rival = _other_headers(client)
        foreign = client.post(
            "/v1/candidates",
            json={"full_name": "Bo Rival", "emails": ["bo@rival-talent.com"]},
            headers=rival,
        )
        assert foreign.status_code == 201, foreign.text
        assert (
            client.patch(
                f"/v1/intake/calls/{call.id}",
                json={"candidate_id": foreign.json()["id"]},
                headers=rival,
            ).status_code
            == 404
        )


class TestPhoneSuppression:
    def test_phone_only_rows_do_not_collide(self, session, account):
        first = suppress(
            session, org_id=account["org_id"], phone="+14155550001", reason="unsubscribe"
        )
        second = suppress(
            session, org_id=account["org_id"], phone="+14155550002", reason="unsubscribe"
        )
        session.commit()
        assert first is not None and second is not None
        assert first.id != second.id
        assert first.email is None and second.email is None
        assert is_suppressed(session, org_id=account["org_id"], phone="+1 415 555 0001")
        assert is_suppressed(session, org_id=account["org_id"], phone="+14155550002")
        assert not is_suppressed(session, org_id=account["org_id"], email="nobody@example.com")

    def test_request_intake_call_honours_a_phone_stop(self, client, session, account):
        from juicebox.intake.providers import FakeVoiceProvider, set_voice_provider
        from juicebox.models import IntakeNumber

        set_voice_provider(FakeVoiceProvider())
        session.add(
            IntakeNumber(
                org_id=account["org_id"],
                provider="retell",
                phone="+16283865454",
                outbound_agent_id="agent_outbound_1",
            )
        )
        suppress(session, org_id=account["org_id"], phone="+14155550123", reason="unsubscribe")
        session.commit()
        try:
            response = client.post(
                "/v1/intake/call-me",
                json={"phone": "+14155550123", "name": "Ada", "email": "fresh@example.com"},
                headers=account["headers"],
            )
            assert response.status_code == 400
            assert "do-not-contact" in response.json()["detail"]
        finally:
            set_voice_provider(None)


class TestRematchAndTimeline:

    def test_timeline_includes_intake_events(self, client, session, account):
        person = _person(session, account["org_id"])
        call = _call(session, account["org_id"], candidate_id=person.id)
        extract_call(session, call)
        session.commit()

        timeline = client.get(
            f"/v1/candidates/{person.id}/timeline", headers=account["headers"]
        ).json()
        types = [event["type"] for event in timeline["events"]]
        assert "intake.extracted" in types
        assert timeline["intake_threads"] == []
        rival = _other_headers(client)
        assert (
            client.get(
                f"/v1/candidates/{person.id}/timeline", headers=rival
            ).status_code
            == 404
        )

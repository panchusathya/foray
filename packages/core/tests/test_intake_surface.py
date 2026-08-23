"""Authed intake read path, provenance, phone suppression, rematch."""

from __future__ import annotations

from sqlalchemy import select

from juicebox.intake.schema import FIELDS
from juicebox.intake.service import extract_call
from juicebox.models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    IntakeFieldAnswer,
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
        "to_number": "+18882600142",
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


class TestFieldAnswers:
    def test_extract_writes_one_row_per_field(self, session, account):
        person = _person(session, account["org_id"])
        call = _call(session, account["org_id"], candidate_id=person.id)
        extract_call(session, call)
        session.commit()

        answers = session.scalars(
            select(IntakeFieldAnswer).where(IntakeFieldAnswer.intake_call_id == call.id)
        ).all()
        assert {row.field_key for row in answers} == {spec.key for spec in FIELDS}
        titles = next(row for row in answers if row.field_key == "spec.job_titles")
        assert titles.asked is True
        assert titles.candidate_id == person.id


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

    def test_count_agrees_with_the_list(self, client, session, account):
        person = _person(session, account["org_id"])
        _call(session, account["org_id"], candidate_id=person.id, review_needed=True)
        _call(
            session,
            account["org_id"],
            candidate_id=None,
            provider_call_id="prov-2",
            review_needed=False,
        )
        session.commit()
        headers = account["headers"]
        listed = client.get("/v1/intake/calls", headers=headers).json()
        counted = client.get("/v1/intake/calls/count", headers=headers).json()["count"]
        assert counted == len(listed) == 2
        assert client.get(
            "/v1/intake/calls/count", headers=headers, params={"review_needed": True}
        ).json()["count"] == 1
        assert client.get(
            "/v1/intake/calls/count", headers=headers, params={"unattributed": True}
        ).json()["count"] == 1

    def test_attribute_writes_the_preference(self, client, session, account):
        call = _call(session, account["org_id"], candidate_id=None)
        extract_call(session, call)
        session.commit()
        assert session.scalars(select(CandidatePreference)).all() == []

        person = _person(session, account["org_id"])
        patched = client.patch(
            f"/v1/intake/calls/{call.id}",
            json={"candidate_id": person.id},
            headers=account["headers"],
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["candidate_id"] == person.id
        pref = client.get(
            f"/v1/intake/preferences/{person.id}", headers=account["headers"]
        )
        assert pref.status_code == 200
        answers = patched.json()["field_answers"]
        assert answers
        assert all(row["candidate_id"] == person.id for row in answers)

    def test_confirm_sets_confirmed_at(self, client, session, account):
        person = _person(session, account["org_id"])
        call = _call(session, account["org_id"], candidate_id=person.id)
        extract_call(session, call)
        session.commit()

        confirmed = client.post(
            f"/v1/intake/preferences/{person.id}/confirm",
            headers=account["headers"],
        )
        assert confirmed.status_code == 200
        assert confirmed.json()["confirmed_at"]
        assert confirmed.json()["confirmed_by"] == account["user_id"]

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


class TestVoiceIntakeScope:
    def test_grid_only_lists_people_with_intake(self, client, session, account):
        called = _person(session, account["org_id"], source_id="called")
        _person(
            session,
            account["org_id"],
            name="Never Called",
            phones=["+14155550999"],
            source_id="quiet",
        )
        call = _call(session, account["org_id"], candidate_id=called.id)
        extract_call(session, call)
        session.commit()

        rows = client.get(
            "/v1/candidates",
            headers=account["headers"],
            params={"scope": "voice_intake"},
        ).json()
        assert [row["id"] for row in rows] == [called.id]
        assert rows[0]["intake_confirmed"] is False
        assert rows[0]["intake_match_count"] == 0

        rival = _other_headers(client)
        assert (
            client.get(
                "/v1/candidates", headers=rival, params={"scope": "voice_intake"}
            ).json()
            == []
        )

    def test_unknown_scope_is_rejected(self, client, account):
        response = client.get(
            "/v1/candidates",
            headers=account["headers"],
            params={"scope": "nonsense"},
        )
        assert response.status_code == 422
        assert "voice_intake" in response.json()["detail"]


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
                provider="fake",
                phone="+18882600142",
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
    def test_rematch_is_not_swallowed_by_match_id(self, client, session, account):
        person = _person(session, account["org_id"])
        response = client.post(
            "/v1/job-matches/rematch",
            headers=account["headers"],
            params={"candidate_id": person.id},
        )
        assert response.status_code == 200, response.text
        assert "matched" in response.json()

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
        rival = _other_headers(client)
        assert (
            client.get(
                f"/v1/candidates/{person.id}/timeline", headers=rival
            ).status_code
            == 404
        )

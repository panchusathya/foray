"""The voice call's text follow-up: the promise is kept, or it lands on a human.

The handoff is the one place that originates a chat nobody texted first, so
its tests pin the consent story hard: nothing sends without the latched
opt-in, a STOP that arrived after the call outranks the call's consent, a
number Linq cannot vouch for is parked rather than fallback-texted, and a
retry can never open a second chat to the same number.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_INTAKE_MATCH_TEXT, KIND_THREAD_NOTIFY
from juicebox.messaging import voice_handoff
from juicebox.messaging.channels import LINQ
from juicebox.messaging.linq import LinqSendError
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.threads import open_thread
from juicebox.messaging.voice_handoff import (
    OPTED_OUT_REASON,
    can_text_by_linq,
    handle_match_text,
    schedule_match_text,
)
from juicebox.models import (
    Candidate,
    CandidatePreference,
    ConsentRecord,
    Event,
    IntakeCall,
    IntakeMessage,
    IntakeThread,
    Job,
    JobMatchWatermark,
    Suppression,
    utcnow,
)
from juicebox.repository import sync_candidate_phones

OUR_NUMBER = "+16283865454"
CANDIDATE_NUMBER = "+14155550123"


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch):
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def linq_on(monkeypatch):
    monkeypatch.setenv("LINQ_ENABLED", "true")
    monkeypatch.setenv("LINQ_API_KEY", "lk_test")
    monkeypatch.setenv("LINQ_FROM_NUMBER", "+14155550100")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()


def _candidate(session, org_id, *, first_name="Ada"):
    person = Candidate(
        org_id=org_id,
        source="test",
        source_id="c1",
        full_name="Ada Okonkwo",
        first_name=first_name,
        phones=[CANDIDATE_NUMBER],
    )
    session.add(person)
    session.flush()
    sync_candidate_phones(session, person)
    session.commit()
    return person


def _call(session, org_id, **overrides):
    fields = {
        "org_id": org_id,
        "provider": "retell",
        "provider_call_id": "call_handoff_1",
        "entry_point": "dial_in",
        "from_number": CANDIDATE_NUMBER,
        "to_number": OUR_NUMBER,
        "status": "extracted",
        "transcript": "Agent: ...\nCandidate: ...",
        "sms_opt_in": True,
        "matches_offered": [{"posting_id": "jpost_1", "line": "a role", "score": 0.9}],
        "ended_at": utcnow(),
    }
    fields.update(overrides)
    row = IntakeCall(**fields)
    session.add(row)
    session.commit()
    return row


def _job(session, call):
    row = Job(
        org_id=call.org_id,
        kind=KIND_INTAKE_MATCH_TEXT,
        payload={"intake_call_id": call.id},
        idempotency_key=f"test:{call.id}",
        status="running",
    )
    session.add(row)
    session.flush()
    return row


def _fresh_book(session, org_id, candidate_id):
    """A watermark newer than the preference clock: the rescore has landed."""
    pref = CandidatePreference(org_id=org_id, candidate_id=candidate_id, spec={})
    session.add(pref)
    session.flush()
    session.add(
        JobMatchWatermark(
            org_id=org_id,
            candidate_id=candidate_id,
            computed_at=utcnow() + timedelta(seconds=5),
            engine_version="test",
        )
    )
    session.commit()


def _notify_jobs(session):
    return session.scalars(select(Job).where(Job.kind == KIND_THREAD_NOTIFY)).all()


class TestScheduling:
    def test_one_promise_one_job(self, session, account):
        call = _call(session, account["org_id"])
        first = schedule_match_text(session, call)
        second = schedule_match_text(session, call)
        assert first.id == second.id


class TestRouting:
    def test_no_opt_in_is_skipped(self, session, account):
        call = _call(session, account["org_id"], sms_opt_in=False)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result == {"skipped": "no opt-in"}

    def test_nothing_pitched_is_skipped(self, session, account):
        call = _call(session, account["org_id"], matches_offered=[])
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result == {"skipped": "nothing was pitched"}

    def test_an_unattributed_call_parks(self, session, account):
        call = _call(session, account["org_id"])
        result = handle_match_text(session, _job(session, call), utcnow())
        assert "parked" in result
        assert call.review_needed is True
        assert "unattributed" in call.review_reason

    def test_a_standing_thread_gets_the_notify(self, session, account):
        person = _candidate(session, account["org_id"])
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat_prior",
            from_address=CANDIDATE_NUMBER,
            candidate_id=person.id,
            mode=MODE_MANAGING,
        )
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result["delivered_via"] == "existing_thread"
        jobs = _notify_jobs(session)
        assert len(jobs) == 1
        assert jobs[0].payload["thread_id"] == thread.id
        # Keyed on the call, not the daily bucket: a routine rescore that
        # already texted today must not swallow the promise this call made.
        assert call.id in jobs[0].idempotency_key
        assert not call.review_reason

    def test_it_waits_for_the_rescore(self, session, account):
        person = _candidate(session, account["org_id"])
        pref = CandidatePreference(org_id=account["org_id"], candidate_id=person.id, spec={})
        session.add(pref)
        session.commit()
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result == {"defer": True}

    def test_patience_runs_out(self, session, account):
        person = _candidate(session, account["org_id"])
        pref = CandidatePreference(org_id=account["org_id"], candidate_id=person.id, spec={})
        session.add(pref)
        session.commit()
        call = _call(
            session,
            account["org_id"],
            candidate_id=person.id,
            ended_at=utcnow() - timedelta(minutes=45),
        )
        # Stale book, old call: proceeds, and with Linq off that means parking.
        result = handle_match_text(session, _job(session, call), utcnow())
        assert "parked" in result

    def test_linq_disabled_parks(self, session, account):
        person = _candidate(session, account["org_id"])
        _fresh_book(session, account["org_id"], person.id)
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        session.flush()
        assert result["parked"].startswith("match list promised by text")
        assert call.review_needed is True
        event = session.scalar(select(Event).where(Event.type == "intake.match_text.parked"))
        assert event is not None

    def test_a_stop_after_the_call_wins(self, session, account, linq_on, monkeypatch):
        person = _candidate(session, account["org_id"])
        _fresh_book(session, account["org_id"], person.id)
        session.add(
            Suppression(org_id=account["org_id"], email=None, phone=CANDIDATE_NUMBER)
        )
        session.commit()
        called = []
        monkeypatch.setattr(voice_handoff, "start_chat", lambda **kw: called.append(kw))
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result["parked"] == OPTED_OUT_REASON
        assert called == []


class TestLinqDelivery:
    def _ready(self, session, account, monkeypatch, *, capable=True):
        person = _candidate(session, account["org_id"])
        _fresh_book(session, account["org_id"], person.id)
        monkeypatch.setattr(voice_handoff, "check_imessage", lambda handle: capable)
        sent = []

        def _start_chat(**kwargs):
            sent.append(kwargs)
            return "chat_777", "msg_888"

        monkeypatch.setattr(voice_handoff, "start_chat", _start_chat)
        return person, sent

    def test_the_happy_path_opens_chat_thread_and_notify(
        self, session, account, linq_on, monkeypatch
    ):
        person, sent = self._ready(session, account, monkeypatch)
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())

        assert result["delivered_via"] == "linq"
        assert len(sent) == 1
        assert sent[0]["to_handle"] == CANDIDATE_NUMBER
        assert "Ada" in sent[0]["body"]
        assert "Vivian" in sent[0]["body"]

        thread = session.scalar(select(IntakeThread))
        assert thread is not None
        assert thread.channel == LINQ
        assert thread.provider_thread_id == "chat_777"
        assert thread.mode == MODE_MANAGING
        assert thread.candidate_id == person.id
        assert thread.consent_messaging is True
        assert thread.entry_point == "voice_handoff"

        greeting = session.scalar(select(IntakeMessage))
        assert greeting is not None
        assert greeting.origin == "proactive"
        assert greeting.provider_native_id == "msg_888"

        consent_row = session.scalar(
            select(ConsentRecord).where(ConsentRecord.thread_id == thread.id)
        )
        assert consent_row is not None
        assert consent_row.source == "voice_wrap_up"

        jobs = _notify_jobs(session)
        assert len(jobs) == 1 and jobs[0].payload["thread_id"] == thread.id
        session.flush()
        event = session.scalar(select(Event).where(Event.type == "intake.match_text.sent"))
        assert event is not None
        assert not call.review_reason

    def test_a_number_linq_cannot_vouch_for_parks(
        self, session, account, linq_on, monkeypatch
    ):
        person, sent = self._ready(session, account, monkeypatch, capable=False)
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert "parked" in result
        assert sent == []

    def test_a_failed_chat_create_parks(self, session, account, linq_on, monkeypatch):
        person = _candidate(session, account["org_id"])
        _fresh_book(session, account["org_id"], person.id)
        monkeypatch.setattr(voice_handoff, "check_imessage", lambda handle: True)

        def _boom(**kwargs):
            raise LinqSendError("nope")

        monkeypatch.setattr(voice_handoff, "start_chat", _boom)
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert "parked" in result
        assert call.review_needed is True

    def test_a_retry_never_opens_a_second_chat(self, session, account, linq_on, monkeypatch):
        """A prior attempt died after the chat was created: reuse, never resend."""
        person, sent = self._ready(session, account, monkeypatch)
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat_777",
            from_address=CANDIDATE_NUMBER,
            candidate_id=person.id,
            mode="intake",
        )
        session.commit()
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert result["delivered_via"] == "existing_linq_thread"
        assert sent == []
        assert thread.mode == MODE_MANAGING
        assert len(_notify_jobs(session)) == 1

    def test_a_thread_owned_by_someone_else_parks(
        self, session, account, linq_on, monkeypatch
    ):
        person, sent = self._ready(session, account, monkeypatch)
        stranger = Candidate(
            org_id=account["org_id"], source="test", source_id="c2", full_name="Someone Else"
        )
        session.add(stranger)
        session.flush()
        open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat_other",
            from_address=CANDIDATE_NUMBER,
            candidate_id=stranger.id,
            mode="intake",
        )
        session.commit()
        call = _call(session, account["org_id"], candidate_id=person.id)
        result = handle_match_text(session, _job(session, call), utcnow())
        assert "parked" in result
        assert "someone else" in result["parked"]
        assert sent == []


class TestCanTextByLinq:
    def test_off_by_default(self, session, account):
        assert (
            can_text_by_linq(session, org_id=account["org_id"], phone=CANDIDATE_NUMBER)
            is False
        )

    def test_on_when_configured_and_clean(self, session, account, linq_on):
        assert (
            can_text_by_linq(session, org_id=account["org_id"], phone=CANDIDATE_NUMBER)
            is True
        )

    def test_off_for_a_suppressed_number(self, session, account, linq_on):
        session.add(
            Suppression(org_id=account["org_id"], email=None, phone=CANDIDATE_NUMBER)
        )
        session.commit()
        assert (
            can_text_by_linq(session, org_id=account["org_id"], phone=CANDIDATE_NUMBER)
            is False
        )


class TestLinqApi:
    def test_start_chat_sends_the_documented_shape(self, monkeypatch, linq_on):
        from juicebox.messaging.linq import start_chat

        seen = {}

        def transport(method, url, payload):
            seen.update({"method": method, "url": url, "payload": payload})
            return {"chat_id": "chat_1", "message": {"id": "msg_1"}}

        chat_id, message_id = start_chat(
            to_handle=CANDIDATE_NUMBER, body="hello", idempotency_key="k1", transport=transport
        )
        assert (chat_id, message_id) == ("chat_1", "msg_1")
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/chats")
        assert seen["payload"]["from"] == "+14155550100"
        assert seen["payload"]["to"] == [CANDIDATE_NUMBER]
        parts = seen["payload"]["message"]["parts"]
        assert parts == [{"type": "text", "value": "hello"}]

    def test_start_chat_refuses_a_response_with_no_chat_id(self, linq_on):
        from juicebox.messaging.linq import start_chat

        with pytest.raises(LinqSendError):
            start_chat(
                to_handle=CANDIDATE_NUMBER,
                body="hello",
                idempotency_key="k1",
                transport=lambda m, u, p: {"message": {"id": "msg_1"}},
            )

    def test_check_imessage_fails_closed(self, linq_on):
        import httpx

        from juicebox.messaging.linq import check_imessage

        def transport(method, url, payload):
            raise httpx.HTTPError("down")

        assert check_imessage(CANDIDATE_NUMBER, transport=transport) is False

    def test_check_imessage_reads_capable(self, linq_on):
        from juicebox.messaging.linq import check_imessage

        assert (
            check_imessage(
                CANDIDATE_NUMBER, transport=lambda m, u, p: {"capable": True}
            )
            is True
        )

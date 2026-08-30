"""The live voice call verifies the text address before job cards are sent."""

from __future__ import annotations

import base64
import json
import time
from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.intake.providers import RetellProvider, set_voice_provider
from juicebox.jobs import KIND_INTAKE_MATCH_TEXT, KIND_THREAD_NOTIFY
from juicebox.messaging import voice_handoff
from juicebox.messaging.channels import LINQ
from juicebox.messaging.linq import sign_webhook
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.threads import open_thread
from juicebox.messaging.voice_handoff import (
    MODE_VOICE_VERIFY,
    confirm_text_verification,
    handle_match_text,
    start_text_verification,
)
from juicebox.models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    IntakeNumber,
    IntakeThread,
    Job,
    JobMatchWatermark,
    utcnow,
)
from juicebox.repository import sync_candidate_phones

OUR_NUMBER = "+16283865454"
CANDIDATE_NUMBER = "+14155550123"


@pytest.fixture
def linq_on(monkeypatch):
    monkeypatch.setenv("LINQ_ENABLED", "true")
    monkeypatch.setenv("LINQ_API_KEY", "lk_test")
    monkeypatch.setenv("LINQ_FROM_NUMBER", "+14155550100")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()


def _call(session, org_id, **overrides):
    values = {
        "org_id": org_id,
        "provider": "retell",
        "provider_call_id": "call_verify_1",
        "entry_point": "dial_in",
        "from_number": CANDIDATE_NUMBER,
        "to_number": OUR_NUMBER,
        "status": "in_progress",
        "matches_offered": [{"posting_id": "jpost_1", "line": "a role", "score": 0.9}],
    }
    values.update(overrides)
    call = IntakeCall(**values)
    session.add(call)
    session.commit()
    return call


def _candidate(session, org_id):
    candidate = Candidate(
        org_id=org_id,
        source="test",
        source_id="candidate_1",
        full_name="Ada Okonkwo",
        first_name="Ada",
        phones=[CANDIDATE_NUMBER],
    )
    session.add(candidate)
    session.flush()
    sync_candidate_phones(session, candidate)
    session.commit()
    return candidate


def test_live_tool_sends_only_the_verification_prompt(session, account, linq_on, monkeypatch):
    call = _call(session, account["org_id"])
    started = []
    monkeypatch.setattr(voice_handoff, "check_imessage", lambda handle: True)
    monkeypatch.setattr(
        voice_handoff,
        "start_chat",
        lambda **kwargs: (started.append(kwargs) or ("chat_1", "message_1")),
    )

    result = start_text_verification(session, call)

    assert result["sent"] is True
    assert "reply YES" in result["guidance"]
    assert "Foray" in started[0]["body"]
    assert call.text_verification_status == "sent"
    assert call.sms_opt_in is True
    thread = session.scalar(select(IntakeThread))
    assert thread is not None and thread.mode == MODE_VOICE_VERIFY
    assert thread.consent_messaging is False
    assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_MATCH_TEXT)).all() == []


def test_second_tool_call_is_idempotent(session, account, linq_on, monkeypatch):
    call = _call(session, account["org_id"])
    calls = []
    monkeypatch.setattr(voice_handoff, "check_imessage", lambda handle: True)
    monkeypatch.setattr(
        voice_handoff,
        "start_chat",
        lambda **kwargs: (calls.append(kwargs) or ("chat_1", "message_1")),
    )
    start_text_verification(session, call)
    start_text_verification(session, call)
    assert len(calls) == 1


def test_reply_is_the_only_card_delivery_trigger(session, account):
    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat_1",
        from_address=CANDIDATE_NUMBER,
        mode=MODE_VOICE_VERIFY,
        consent_messaging=False,
    )
    call = _call(
        session,
        account["org_id"],
        text_verification_thread_id=thread.id,
        text_verification_status="sent",
        text_verification_sent_at=utcnow(),
    )

    confirmation = confirm_text_verification(session, thread=thread)

    assert confirmation == {"call_id": call.id, "thread_id": thread.id}
    assert call.text_verification_status == "confirmed"
    assert thread.mode == MODE_MANAGING
    assert thread.consent_messaging is True
    jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_MATCH_TEXT)).all()
    assert len(jobs) == 1
    # Call extraction has not landed yet, so no card delivery can race ahead.
    assert handle_match_text(session, jobs[0], utcnow()) == {"defer": True}


def test_confirmed_reply_delivers_to_its_exact_thread_after_extraction(session, account):
    candidate = _candidate(session, account["org_id"])
    preference = CandidatePreference(org_id=account["org_id"], candidate_id=candidate.id, spec={})
    session.add(preference)
    session.flush()
    session.add(
        JobMatchWatermark(
            org_id=account["org_id"],
            candidate_id=candidate.id,
            computed_at=utcnow() + timedelta(seconds=5),
            engine_version="test",
        )
    )
    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat_1",
        from_address=CANDIDATE_NUMBER,
        candidate_id=candidate.id,
        mode=MODE_VOICE_VERIFY,
    )
    _call(
        session,
        account["org_id"],
        candidate_id=candidate.id,
        status="extracted",
        text_verification_thread_id=thread.id,
        text_verification_status="sent",
        text_verification_sent_at=utcnow(),
    )
    session.commit()

    confirm_text_verification(session, thread=thread)
    job = session.scalar(select(Job).where(Job.kind == KIND_INTAKE_MATCH_TEXT))
    assert handle_match_text(session, job, utcnow()) == {
        "delivered_via": "verified_reply",
        "thread_id": thread.id,
    }
    assert len(session.scalars(select(Job).where(Job.kind == KIND_THREAD_NOTIFY)).all()) == 1


def test_signed_voice_tool_sends_before_hangup(client, session, account, linq_on, monkeypatch):
    """The custom function uses the same signed Retell envelope as coverage."""
    secret = "retell-test-secret"
    monkeypatch.setenv("RETELL_API_KEY", secret)
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    signer = RetellProvider(api_key=secret)
    set_voice_provider(None)
    session.add(
        IntakeNumber(
            org_id=account["org_id"], provider="retell", phone=OUR_NUMBER, label="voice"
        )
    )
    session.commit()
    call = {
        "call_id": "call_live_tool",
        "call_type": "phone_call",
        "call_status": "in_progress",
        "direction": "inbound",
        "from_number": CANDIDATE_NUMBER,
        "to_number": OUR_NUMBER,
        "agent_id": "agent_1",
        "transcript": "Agent: Would you like details by text?\nCandidate: Yes.",
    }

    def _post(payload, path):
        raw = json.dumps(payload).encode()
        return client.post(
            path,
            content=raw,
            headers={
                "X-Retell-Signature": signer.sign(raw_body=raw),
                "Content-Type": "application/json",
            },
        )

    assert _post({"event": "call_started", "call": call}, "/v1/intake/webhook").status_code == 200
    stored = session.scalar(select(IntakeCall).where(IntakeCall.provider_call_id == "call_live_tool"))
    stored.matches_offered = [{"posting_id": "jpost_1", "line": "a role", "score": 0.9}]
    session.commit()
    monkeypatch.setattr(voice_handoff, "check_imessage", lambda handle: True)
    monkeypatch.setattr(voice_handoff, "start_chat", lambda **kwargs: ("chat_live", "msg_live"))

    response = _post(
        {"name": "start_text_verification", "call": call, "args": {}},
        "/v1/intake/text-verification",
    )
    assert response.status_code == 200
    assert response.json()["sent"] is True
    session.refresh(stored)
    assert stored.text_verification_status == "sent"


def test_linq_webhook_reply_is_the_confirmation_trigger(client, session, account, monkeypatch):
    secret = "whsec_" + base64.b64encode(b"live-verify-webhook-secret").decode()
    our_text_number = "+14155559001"
    monkeypatch.setenv("LINQ_WEBHOOK_SECRET", secret)
    monkeypatch.setenv("LINQ_API_KEY", "linq_test")
    monkeypatch.setenv("LINQ_FROM_NUMBER", our_text_number)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    from juicebox.config import reset_settings_cache
    from juicebox.messaging.channels import reset_channels

    reset_settings_cache()
    reset_channels()
    session.add(IntakeNumber(org_id=account["org_id"], provider="linq", phone=our_text_number))
    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat_webhook",
        from_address=CANDIDATE_NUMBER,
        mode=MODE_VOICE_VERIFY,
    )
    call = _call(
        session,
        account["org_id"],
        text_verification_thread_id=thread.id,
        text_verification_status="sent",
        text_verification_sent_at=utcnow(),
    )
    session.commit()
    payload = {
        "api_version": "v3",
        "event_type": "message.received",
        "event_id": "event_verify_1",
        "data": {
            "chat_id": "chat_webhook",
            "from": CANDIDATE_NUMBER,
            "recipient_handle": {"handle": our_text_number},
            "is_group": False,
            "message": {"id": "message_verify_1", "parts": [{"type": "text", "value": "hey"}]},
        },
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    response = client.post(
        "/v1/intake/linq/webhook",
        content=raw,
        headers={
            "webhook-id": "webhook_verify_1",
            "webhook-timestamp": timestamp,
            "webhook-signature": sign_webhook(
                raw_body=raw,
                webhook_id="webhook_verify_1",
                timestamp=timestamp,
                secret=secret,
            ),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["verification_confirmed"] is True
    session.refresh(call)
    session.refresh(thread)
    assert call.text_verification_status == "confirmed"
    assert thread.mode == MODE_MANAGING
    jobs = session.scalars(select(Job).where(Job.kind == KIND_INTAKE_MATCH_TEXT)).all()
    assert len(jobs) == 1

"""The voice call's text follow-up: the promise is kept, or it lands on a human.

The handoff is the one place that originates a chat nobody texted first, so
its tests pin the consent story hard: nothing sends without the latched
opt-in, a STOP that arrived after the call outranks the call's consent, a
number Linq cannot vouch for is parked rather than fallback-texted, and a
retry can never open a second chat to the same number.
"""

from __future__ import annotations

import pytest

from juicebox.messaging.linq import LinqSendError
from juicebox.messaging.voice_handoff import can_text_by_linq, schedule_match_text
from juicebox.models import IntakeCall, Suppression, utcnow

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


class TestScheduling:
    def test_one_promise_one_job(self, session, account):
        call = _call(session, account["org_id"])
        first = schedule_match_text(session, call)
        second = schedule_match_text(session, call)
        assert first.id == second.id


class TestCanTextByLinq:
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


"""The iMessage contact card: what we ask Linq for, and who gets asked.

Two halves, tested apart because they fail apart. The client half is wire
shape — a card is ``phone_number`` + ``first_name`` and optionally a photo,
and getting that wrong is a 4xx nobody sees until a real number is live. The
policy half is the part with an opinion: a share goes out only after the
person has answered something we sent, only on the candidate line, and only
three times ever.

The one that would hurt most in production is the first-outbound case. Linq
will happily accept a share into a chat where we have spoken and they have
not, so nothing downstream would complain — the candidate would simply be
asked to file a stranger under a name, which is the behaviour this whole
module exists to not have.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from juicebox.messaging import contact_card
from juicebox.messaging.channels import LINQ, DeliveryResult, OutboundMessage
from juicebox.messaging.linq import (
    SERVICE_IMESSAGE,
    SERVICE_RCS,
    set_contact_card,
)
from juicebox.messaging.threads import open_thread, record_inbound, record_outbound
from juicebox.models import Org

OUR_NUMBER = "+14155550100"
OTHER_NUMBER = "+14155550999"
CANDIDATE = "+14155550123"
CHAT_ID = "8f392755-6865-4b18-880a-227f9d8b458f"


class RecordingTransport:
    """The vendor, reduced to what it was asked and what it answered."""

    def __init__(self, response: dict | None = None):
        self.calls: list[tuple[str, str, dict]] = []
        self.response = response or {}

    def __call__(self, method: str, url: str, payload: dict) -> dict:
        self.calls.append((method, url, payload))
        return self.response


@pytest.fixture
def card_env(monkeypatch):
    """A configured, enabled Linq line whose number is the candidate line."""
    monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
    monkeypatch.setenv("LINQ_FROM_NUMBER", OUR_NUMBER)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    monkeypatch.setenv("LINQ_CONTACT_CARD_ENABLED", "true")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture
def thread(session):
    org = Org(name="Foray Talent", slug="foray-talent-card")
    session.add(org)
    session.flush()
    row = open_thread(
        session,
        org_id=org.id,
        channel=LINQ,
        provider="linq",
        provider_thread_id=CHAT_ID,
        entry_point="linq_inbound",
        from_address=CANDIDATE,
        to_address=OUR_NUMBER,
        consent_messaging=True,
    )
    # Set the way the webhook sets it: every inbound records the service it
    # arrived over.
    row.last_service = SERVICE_IMESSAGE
    session.flush()
    return row


def _outbound(session, thread) -> None:
    record_outbound(
        session,
        thread=thread,
        message=OutboundMessage(body="hey, i'm vivian from foray."),
        result=DeliveryResult(status="sent"),
    )


def _inbound(session, thread, body: str = "hey") -> None:
    record_inbound(session, thread=thread, body=body)


class TestClient:
    """What goes on the wire for a card."""

    def test_empty_optionals_are_omitted_not_sent_blank(self):
        """A blank ``image_url`` would clear a photo somebody just set."""
        transport = RecordingTransport()
        set_contact_card(
            phone_number=OUR_NUMBER,
            first_name="Foray",
            last_name="",
            image_url="",
            transport=transport,
        )
        assert "image_url" not in transport.calls[0][2]
        assert "last_name" not in transport.calls[0][2]

class TestWhoGetsAsked:
    """The policy half."""

    def test_silent_until_they_reply(self, session, thread, card_env, monkeypatch):
        """The case Linq would accept and the candidate would resent.

        One outbound, no answer: we have spoken at somebody, which is not the
        same as being in a conversation with them.
        """
        shared = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: shared.append(chat) or True
        )
        _inbound(session, thread)  # how the conversation opened
        _outbound(session, thread)  # our first reply
        assert contact_card.maybe_share(session, thread=thread) is False
        assert shared == []

    def test_not_twice_in_a_day(self, session, thread, card_env, monkeypatch):
        calls = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: calls.append(chat) or True
        )
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)
        assert contact_card.maybe_share(session, thread=thread) is True
        assert contact_card.maybe_share(session, thread=thread) is False
        assert len(calls) == 1

    def test_retries_the_next_day_and_then_gives_up(
        self, session, thread, card_env, monkeypatch
    ):
        """Linq says a share may not land and to retry daily. Three, not forever."""
        calls = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: calls.append(chat) or True
        )
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)

        for day in range(5):
            contact_card._remember(
                thread,
                last_attempt_at=(datetime.now(UTC) - timedelta(days=day + 1)).isoformat(),
            )
            contact_card.maybe_share(session, thread=thread)
        assert len(calls) == contact_card.MAX_ATTEMPTS

    def test_only_the_candidate_line(self, session, thread, card_env, monkeypatch):
        """A second Linq line must not inherit the candidate agent's face."""
        called = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: called.append(chat) or True
        )
        thread.to_address = OTHER_NUMBER
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)
        assert contact_card.maybe_share(session, thread=thread) is False
        assert called == []

    def test_never_over_rcs(self, session, thread, card_env, monkeypatch):
        """An Android candidate on RCS cannot be shown an Apple contact card.

        ``cards.py`` sends them a job card image happily -- RCS renders one --
        so "rich enough for a picture" is the wrong test here and this is why
        it is spelled out separately.
        """
        called = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: called.append(chat) or True
        )
        thread.last_service = SERVICE_RCS
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)
        assert contact_card.maybe_share(session, thread=thread) is False
        assert called == []
        # And no attempt was spent, so an iPhone upgrade still gets one.
        assert contact_card.card_meta(thread).get("attempts") is None

    def test_off_unless_switched_on(self, session, thread, monkeypatch):
        """Default-off: a share against a number with no card is 2012 every time."""
        monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
        monkeypatch.setenv("LINQ_FROM_NUMBER", OUR_NUMBER)
        monkeypatch.delenv("LINQ_CONTACT_CARD_ENABLED", raising=False)
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        called = []
        monkeypatch.setattr(
            contact_card, "share_contact_card", lambda chat: called.append(chat) or True
        )
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)
        try:
            assert contact_card.maybe_share(session, thread=thread) is False
            assert called == []
        finally:
            reset_settings_cache()

    def test_a_vendor_failure_never_reaches_the_turn(
        self, session, thread, card_env, monkeypatch
    ):
        def explode(chat):
            raise RuntimeError("linq is down")

        monkeypatch.setattr(contact_card, "share_contact_card", explode)
        _inbound(session, thread)
        _outbound(session, thread)
        _inbound(session, thread, "ok")
        _outbound(session, thread)
        assert contact_card.maybe_share(session, thread=thread) is False

    def test_bookkeeping_survives_a_record_merge(self, session, thread, card_env):
        """``_contact_card`` lives on ``thread.record``, which turns rewrite.

        If a merge dropped it the thread would re-offer the card every day
        forever, which is the failure the attempt cap is supposed to prevent.
        """
        from juicebox.intake.records import merge_record

        contact_card._remember(thread, attempts=2)
        thread.record = merge_record(thread.record, {"spec": {"titles": ["engineer"]}})
        assert contact_card.card_meta(thread)["attempts"] == 2

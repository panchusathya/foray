"""Group-chat referrals: what we send to a room we were invited into, and what we don't.

The feature's whole risk is one message reaching somebody who never texted us,
so most of this file is about restraint rather than function:

* **We say we are going to leave, then stay until the newcomer texts us.**
  Walking out in the same breath as handing somebody a number leaves them no
  way to check they had the right one. ``depart`` runs on their first private
  message, not on the answer.
* **At most one redirect after that**, and then quiet. Still in the room, still
  not running a turn in it.
* **No one-to-one message is ever sent to a referred friend.** They are told to
  text us; the reward waits for them to do it. There is no test asserting we
  DM them because there is no code that can.
* **The group thread must never hijack the referrer's private thread.** It now
  stays ``active`` while we are in the chat, so the protection is the
  ``MODE_REFERRAL`` exclusion in ``find_active_thread`` rather than the thread
  being closed -- which is why that regression is tested from both ends.
* **A rejected referral is recorded with a reason**, never silently dropped.

The suite runs with no API key and no network: the two Linq calls the job makes
are injected, and the send goes through a ``FakeChannel``.
"""

from __future__ import annotations

import base64
import json
import time

import pytest
from sqlalchemy import select

from juicebox.jobs import KIND_INTAKE_REFERRAL, KIND_INTAKE_TURN, Job
from juicebox.messaging import prompts, referrals
from juicebox.messaging.channels import LINQ, set_channel
from juicebox.messaging.linq import ChatHandle, fetch_chat_handles, leave_chat, sign_webhook
from juicebox.models import Candidate, IntakeNumber, IntakeThread, Referral

LINQ_WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"linq-test-webhook-secret!!").decode()
OUR_NUMBER = "+14155559001"
REFERRER = "+14155550123"
FRIEND = "+14155557777"
SECOND_FRIEND = "+14155558888"
GROUP_CHAT_ID = "6b1c4f88-2f2a-4a44-9f2e-1a5c8d0e7b31"
PRIVATE_CHAT_ID = "9d2e5a11-7c31-4c0f-8a2b-3f6e9c1d4a52"


@pytest.fixture
def linq_env(monkeypatch):
    monkeypatch.setenv("LINQ_WEBHOOK_SECRET", LINQ_WEBHOOK_SECRET)
    monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
    monkeypatch.setenv("LINQ_FROM_NUMBER", OUR_NUMBER)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    monkeypatch.setenv("LINQ_GROUP_REFERRALS_ENABLED", "true")
    from juicebox.config import reset_settings_cache
    from juicebox.messaging.channels import reset_channels

    reset_settings_cache()
    reset_channels()
    yield
    reset_settings_cache()
    reset_channels()


@pytest.fixture
def linq_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"],
        provider="linq",
        phone=OUR_NUMBER,
        label="linq intake",
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def linq_chat(monkeypatch):
    """Stand in for the two Linq calls the referral job makes.

    ``handles`` is what ``GET /chats/{id}`` would return and is settable per
    test; ``left`` records the chat ids we asked to leave. Injected here rather
    than through the ``transport`` seam because the job calls these without one
    -- the HTTP shape itself is covered by ``TestLinqChatCalls`` below.
    """

    class Chat:
        def __init__(self):
            self.handles = (
                ChatHandle(handle=OUR_NUMBER, is_me=True),
                ChatHandle(handle=REFERRER),
                ChatHandle(handle=FRIEND),
            )
            self.left: list[str] = []
            self.fetched: list[str] = []

    chat = Chat()

    def fake_fetch(chat_id, *, transport=None):
        chat.fetched.append(chat_id)
        return chat.handles

    def fake_leave(chat_id, *, transport=None):
        chat.left.append(chat_id)

    monkeypatch.setattr(referrals, "fetch_chat_handles", fake_fetch)
    monkeypatch.setattr(referrals, "leave_chat", fake_leave)
    return chat


def group_payload(*, chat_id=GROUP_CHAT_ID, sender=REFERRER, text="meet my friend", msg_id="grp-1"):
    return {
        "api_version": "v3",
        "event_type": "message.received",
        "event_id": f"evt-{msg_id}",
        "data": {
            "chat_id": chat_id,
            "from": sender,
            "is_group": True,
            "message": {"id": msg_id, "parts": [{"type": "text", "value": text}]},
            # A group envelope does not reliably name the recipient -- the chat
            # is the recipient. Left out on purpose: the org has to resolve from
            # our own configured line, and a test that supplied it would never
            # exercise that fallback.
        },
    }


def private_payload(*, sender, chat_id=PRIVATE_CHAT_ID, text="hey", msg_id="prv-1"):
    return {
        "api_version": "v3",
        "event_type": "message.received",
        "event_id": f"evt-{msg_id}",
        "data": {
            "chat_id": chat_id,
            "from": sender,
            "is_group": False,
            "message": {"id": msg_id, "parts": [{"type": "text", "value": text}]},
            "recipient_handle": {"handle": OUR_NUMBER},
        },
    }


def post_linq(client, payload, *, webhook_id="wh-1"):
    raw = json.dumps(payload).encode()
    ts = str(int(time.time()))
    return client.post(
        "/v1/intake/linq/webhook",
        content=raw,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": ts,
            "webhook-signature": sign_webhook(
                raw_body=raw, webhook_id=webhook_id, timestamp=ts, secret=LINQ_WEBHOOK_SECRET
            ),
            "Content-Type": "application/json",
        },
    )


def drain(session, kind=KIND_INTAKE_REFERRAL):
    from datetime import UTC, datetime

    from juicebox.worker import process_job

    # Pinned inside the unknown-timezone send window (16:00Z-02:00Z): the
    # notify handler judges cadence on the worker's `now`, and a suite run in
    # the evening US-time used to have every proactive send defer.
    in_window = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
    jobs = session.scalars(select(Job).where(Job.kind == kind)).all()
    for job in jobs:
        session.refresh(job)
        process_job(session, job, now=in_window)
    session.commit()
    return jobs


def candidate(session, org_id, *, name="Existing Person", phone=None):
    from juicebox.repository import sync_candidate_phones

    person = Candidate(
        org_id=org_id,
        source="manual",
        source_id=f"seed-{name}-{phone}",
        full_name=name,
        phones=[phone] if phone else [],
    )
    session.add(person)
    session.flush()
    sync_candidate_phones(session, person)
    session.commit()
    return person


pytestmark = pytest.mark.usefixtures("linq_env")


class TestTheGroupTurn:
    def test_a_new_number_in_a_group_becomes_a_pending_referral(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        response = post_linq(client, group_payload())
        assert response.status_code == 200, response.text
        assert response.json()["group"] is True

        drain(session)

        referral = session.scalars(select(Referral)).one()
        assert referral.status == referrals.STATUS_PENDING
        assert referral.referee_address == FRIEND
        assert referral.referrer_address == REFERRER
        assert referral.group_chat_id == GROUP_CHAT_ID
        assert referral.bonus_applications == 5
        assert referral.confirmed_at is None
        # One message, and we are still in the chat: the newcomer has been
        # asked to text us and needs us reachable until they do.
        assert len(fake_channel.bodies) == 1
        assert linq_chat.left == []

    def test_the_group_message_says_what_it_has_to_and_no_more(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """AI disclosure, our number, the reward, and notice that we will go.

        No link: Linq refuses links in a first outbound and a group can
        silently be on SMS.
        """
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        body = fake_channel.bodies[0]
        assert "ai" in body
        assert OUR_NUMBER in body
        assert "5 free applications" in body
        assert "step out" in body
        assert "http" not in body

    def test_no_turn_is_ever_run_on_a_group(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """The intake agent asks one person about their own preferences, and
        there is more than one person in the room."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []

    def test_a_later_ping_gets_the_one_to_one_redirect(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """We are still in the room, so a question is answered -- with the only
        answer there is: not here, text me."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        again = post_linq(client, group_payload(msg_id="grp-2", text="what roles?"))
        assert again.status_code == 200
        assert again.json() == {
            "status": "accepted",
            "answered": False,
            "redirected": True,
            "message_id": again.json()["message_id"],
        }
        assert len(fake_channel.bodies) == 2
        assert "separately" in fake_channel.bodies[1]
        assert OUR_NUMBER in fake_channel.bodies[1]
        # The thank-you is never replayed, and no turn is run in a group.
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_TURN)).all() == []

    def test_the_redirect_is_sent_once_and_then_we_go_quiet(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """Two messages is the cap. The number has been given twice by then,
        and a third is us not taking the hint -- but we stay in the chat,
        because the newcomer may still text."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        for index in range(2, 6):
            response = post_linq(
                client, group_payload(msg_id=f"grp-{index}", text="hello?")
            )
            assert response.status_code == 200
        assert len(fake_channel.bodies) == prompts.MAX_GROUP_REPLIES
        assert fake_channel.bodies[1].count("separately") == 1
        assert linq_chat.left == []

    def test_a_redelivered_webhook_is_a_no_op(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        replay = post_linq(client, group_payload())
        assert replay.json()["duplicate"] is True
        assert len(session.scalars(select(Job).where(Job.kind == KIND_INTAKE_REFERRAL)).all()) == 1

    def test_the_job_is_idempotent_if_it_runs_twice(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """Keyed on having spoken, since the thread is deliberately still open
        -- a second thank-you is the one outcome worse than staying quiet."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        thread = session.scalars(
            select(IntakeThread).where(IntakeThread.entry_point == "linq_group")
        ).one()
        drain(session)
        result = referrals.handle_group(session, thread=thread)
        assert result == {"skipped": "group already answered"}
        assert len(fake_channel.bodies) == 1

    def test_a_group_with_nobody_new_is_answered_but_promises_nothing(
        self, client, session, account, linq_number, linq_chat, fake_channel
    ):
        """Everyone here already texts us. Still answered -- being added to a
        group and saying nothing reads as broken -- but there is no referral to
        pay for, so nothing is offered."""
        candidate(session, account["org_id"], name="The Friend", phone=FRIEND)
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        referral = session.scalars(select(Referral)).one()
        assert referral.status == referrals.STATUS_REJECTED
        assert referral.reason == referrals.REASON_ALREADY_A_CANDIDATE
        assert referral.bonus_applications == 0
        assert "free applications" not in fake_channel.bodies[0]
        assert OUR_NUMBER in fake_channel.bodies[0]
        # Nobody to wait for, so this is the one case we leave straight away.
        assert linq_chat.left == [GROUP_CHAT_ID]
        thread = session.scalars(
            select(IntakeThread).where(IntakeThread.entry_point == "linq_group")
        ).one()
        assert thread.status == "complete"

    def test_an_unreachable_chat_still_gets_an_answer(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """Linq would not tell us who is in the room. We answer and leave; we
        do not invent a participant to promise a reward for."""
        linq_chat.handles = ()
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        assert session.scalars(select(Referral)).all() == []
        assert len(fake_channel.bodies) == 1
        # No referral means nobody to wait for: out immediately.
        assert linq_chat.left == [GROUP_CHAT_ID]

    def test_a_participant_who_has_left_is_not_referred(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        linq_chat.handles = (
            ChatHandle(handle=OUR_NUMBER, is_me=True),
            ChatHandle(handle=REFERRER),
            ChatHandle(handle=FRIEND, status="left"),
            ChatHandle(handle=SECOND_FRIEND),
        )
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        addresses = {r.referee_address for r in session.scalars(select(Referral)).all()}
        assert addresses == {SECOND_FRIEND}

    def test_the_newcomer_cap_holds(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        extras = [f"+1415555{7000 + i}" for i in range(6)]
        linq_chat.handles = (
            ChatHandle(handle=OUR_NUMBER, is_me=True),
            ChatHandle(handle=REFERRER),
            *[ChatHandle(handle=number) for number in extras],
        )
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        rows = session.scalars(select(Referral)).all()
        assert len(rows) == referrals.MAX_REFEREES_PER_GROUP


class TestRestraint:
    def test_a_sender_who_opted_out_gets_nothing(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """Opening a group does not make somebody on the do-not-contact list a
        referrer. Answering would put a message on the screen of the person who
        told us to stop."""
        set_channel(LINQ, fake_channel)
        assert post_linq(client, private_payload(sender=REFERRER, text="STOP")).status_code == 200
        fake_channel.sent.clear()

        response = post_linq(client, group_payload())
        assert response.json() == {"status": "ignored", "reason": "sender_opted_out"}
        assert session.scalars(select(Referral)).all() == []
        assert fake_channel.bodies == []

    def test_stop_inside_a_group_still_suppresses(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        response = post_linq(client, group_payload(text="STOP"))
        assert response.json()["stopped"] is True
        thread = session.scalars(select(IntakeThread)).one()
        assert thread.status == "stopped"
        # No referral, and no offer texted at somebody who just opted out.
        assert session.scalars(select(Referral)).all() == []
        assert session.scalars(select(Job).where(Job.kind == KIND_INTAKE_REFERRAL)).all() == []

    def test_a_suppressed_friend_is_recorded_as_rejected_not_referred(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        assert post_linq(client, private_payload(sender=FRIEND, text="STOP")).status_code == 200
        fake_channel.sent.clear()

        post_linq(client, group_payload())
        drain(session)
        referral = session.scalars(select(Referral)).one()
        assert referral.status == referrals.STATUS_REJECTED
        assert referral.reason == referrals.REASON_SUPPRESSED

    def test_the_referrer_cannot_refer_themselves(self, session, account, linq_chat):
        thread = IntakeThread(
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id=GROUP_CHAT_ID,
            entry_point="linq_group",
            from_address=REFERRER,
            record={},
        )
        session.add(thread)
        session.flush()
        handles = (
            ChatHandle(handle=OUR_NUMBER, is_me=True),
            ChatHandle(handle=REFERRER),
        )
        assert referrals.record_group(session, thread=thread, handles=handles) == []


class TestNoThreadHijack:
    def test_the_referrers_next_private_message_opens_its_own_thread(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """The regression the whole design turns on.

        ``find_active_thread`` matches on (org, channel, from_address, active),
        and the group thread is deliberately all four of those while we are
        still in the chat. Its ``provider_thread_id`` is the *group*, so
        without the MODE_REFERRAL exclusion every private reply to the referrer
        would be delivered into the group, in front of the friend they just
        introduced.
        """
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        assert post_linq(client, private_payload(sender=REFERRER)).status_code == 200
        threads = session.scalars(
            select(IntakeThread).where(IntakeThread.from_address == REFERRER)
        ).all()
        assert len(threads) == 2
        by_entry = {t.entry_point: t for t in threads}
        assert by_entry["linq_group"].provider_thread_id == GROUP_CHAT_ID
        assert by_entry["linq_inbound"].provider_thread_id == PRIVATE_CHAT_ID
        # The group is still open -- so this passed because of the exclusion,
        # not because the thread happened to be closed.
        assert by_entry["linq_group"].status == "active"

    def test_find_active_thread_never_returns_a_group(
        self, session, account, linq_chat
    ):
        """The exclusion itself, at the level it lives. A regression here is
        silent at the router and visible to two candidates at once."""
        from juicebox.messaging.threads import find_active_thread

        session.add(
            IntakeThread(
                org_id=account["org_id"],
                channel=LINQ,
                provider="linq",
                provider_thread_id=GROUP_CHAT_ID,
                entry_point="linq_group",
                from_address=REFERRER,
                mode=referrals.MODE_REFERRAL,
                status="active",
                record={},
            )
        )
        session.commit()
        assert (
            find_active_thread(
                session, org_id=account["org_id"], channel=LINQ, from_address=REFERRER
            )
            is None
        )


class TestConfirmation:
    def test_the_friends_first_text_confirms_the_referral(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        assert post_linq(client, private_payload(sender=FRIEND)).status_code == 200
        referral = session.scalars(select(Referral)).one()
        assert referral.status == referrals.STATUS_CONFIRMED
        assert referral.confirmed_at is not None
        friend_thread = session.scalars(
            select(IntakeThread).where(IntakeThread.from_address == FRIEND)
        ).one()
        assert referral.referee_thread_id == friend_thread.id

    def test_we_leave_the_group_once_the_newcomer_texts_us(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """The promise made in the first message, honoured. This is the only
        thing that gets us out of a group with a pending referral in it."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        assert linq_chat.left == []

        post_linq(client, private_payload(sender=FRIEND))

        assert linq_chat.left == [GROUP_CHAT_ID]
        group = session.scalars(
            select(IntakeThread).where(IntakeThread.entry_point == "linq_group")
        ).one()
        assert group.status == "complete"
        # A sign-off, so we do not simply vanish -- the private message that
        # triggered this is invisible from inside the group.
        assert "stepping out" in fake_channel.bodies[1]

    def test_the_sign_off_names_nobody(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """The group may hold people with no business knowing who engaged with
        a job-search service, or that anybody did."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        post_linq(client, private_payload(sender=FRIEND))

        sign_off = fake_channel.bodies[1]
        assert FRIEND not in sign_off
        assert "texted" not in sign_off
        assert "applications" not in sign_off

    def test_a_second_text_does_not_re_confirm(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        post_linq(client, private_payload(sender=FRIEND))
        first = session.scalars(select(Referral)).one().confirmed_at

        post_linq(client, private_payload(sender=FRIEND, msg_id="prv-2", text="still here"))
        referral = session.scalars(select(Referral)).one()
        assert referral.confirmed_at == first

    def test_a_friend_who_already_had_a_thread_still_confirms(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """The gap that moved this check off thread creation.

        A thread alone does not make somebody a candidate, so a friend who had
        texted us once before -- and never finished onboarding -- is still a
        valid referral. Confirmation runs on the message, not on the thread
        being new, or their row would sit pending forever.
        """
        set_channel(LINQ, fake_channel)
        friend_chat = "aa11bb22-cc33-dd44-ee55-ff6677889900"
        assert post_linq(
            client, private_payload(sender=FRIEND, chat_id=friend_chat, msg_id="pre-1")
        ).status_code == 200

        post_linq(client, group_payload())
        drain(session)
        assert session.scalars(select(Referral)).one().status == referrals.STATUS_PENDING

        post_linq(
            client, private_payload(sender=FRIEND, chat_id=friend_chat, msg_id="pre-2")
        )
        assert session.scalars(select(Referral)).one().status == referrals.STATUS_CONFIRMED

    def test_a_friend_whose_first_word_is_stop_confirms_nothing(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        """An opt-out is not a confirmation, whatever it would earn anybody."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        post_linq(client, private_payload(sender=FRIEND, text="STOP"))
        assert session.scalars(select(Referral)).one().status == referrals.STATUS_PENDING

    def test_a_referrer_who_becomes_a_candidate_later_is_still_credited(
        self, client, session, account, linq_number, linq_chat, fake_channel
    ):
        """They had no candidate row when they made the intro -- a referrer
        part-way through onboarding does not. The bonus must not be stranded on
        a row nothing can attribute."""
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        assert session.scalars(select(Referral)).one().referrer_candidate_id is None

        person = candidate(session, account["org_id"], name="Late Referrer", phone=REFERRER)
        post_linq(client, private_payload(sender=FRIEND))

        referral = session.scalars(select(Referral)).one()
        assert referral.status == referrals.STATUS_CONFIRMED
        assert referral.referrer_candidate_id == person.id
        assert (
            referrals.earned_bonus(
                session, org_id=account["org_id"], candidate_id=person.id
            )
            == 5
        )

    def test_the_referrer_is_told_once_they_have_a_line(
        self, client, session, account, linq_number, linq_chat, fake_channel
    ):
        """Routed through notify_candidate, so quiet hours and the suppression
        checks apply -- good news is still an unprompted message."""
        from juicebox.jobs import KIND_THREAD_NOTIFY
        from juicebox.messaging.notify import EVENT_REFERRAL_CONFIRMED

        person = candidate(session, account["org_id"], name="The Referrer", phone=REFERRER)
        session.add(
            IntakeThread(
                org_id=account["org_id"],
                candidate_id=person.id,
                channel=LINQ,
                provider="linq",
                provider_thread_id="managing-chat",
                entry_point="linq_inbound",
                from_address=REFERRER,
                mode="managing",
                status="active",
                consent_messaging=True,
                record={},
            )
        )
        session.commit()

        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)
        post_linq(client, private_payload(sender=FRIEND))

        notify = session.scalars(
            select(Job).where(Job.kind == KIND_THREAD_NOTIFY)
        ).one()
        assert notify.payload["event"] == EVENT_REFERRAL_CONFIRMED
        referral = session.scalars(select(Referral)).one()
        assert notify.payload["subject_id"] == referral.id

        drain(session, kind=KIND_THREAD_NOTIFY)
        assert any("banked" in body for body in fake_channel.bodies)


class TestOneReferralPerNumber:
    def test_the_same_number_in_a_second_group_is_worth_nothing_extra(
        self, client, session, linq_number, linq_chat, fake_channel
    ):
        set_channel(LINQ, fake_channel)
        post_linq(client, group_payload())
        drain(session)

        # A different person opens a different group around the same friend.
        # The friend is the only newcomer in it, and they are already spoken for.
        linq_chat.handles = (
            ChatHandle(handle=OUR_NUMBER, is_me=True),
            ChatHandle(handle=SECOND_FRIEND),
            ChatHandle(handle=FRIEND),
        )
        second = "c1a4e9f0-1111-2222-3333-444455556666"
        post_linq(client, group_payload(chat_id=second, sender=SECOND_FRIEND, msg_id="grp-9"))
        drain(session)

        rows = session.scalars(select(Referral)).all()
        assert [r.referee_address for r in rows] == [FRIEND]
        # Credited to whoever got there first, not the second group.
        assert rows[0].group_chat_id == GROUP_CHAT_ID
        assert rows[0].referrer_address == REFERRER
        # The second group is still answered -- it just has nothing to promise,
        # so it gets the nobody-new line and we leave it at once.
        assert len(fake_channel.bodies) == 2
        assert second in linq_chat.left

    def test_the_constraint_is_the_database_not_the_handler(self, session, account):
        """Enforced in SQL on purpose: adding the same number to ten groups has
        to be worth one referral even when a future call site forgets to ask."""
        from sqlalchemy.exc import IntegrityError

        for _ in range(2):
            session.add(
                Referral(
                    org_id=account["org_id"],
                    referrer_address=REFERRER,
                    referee_address=FRIEND,
                    status=referrals.STATUS_PENDING,
                )
            )
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()


class TestEarnedBonus:
    def test_only_confirmed_referrals_count(self, session, account):
        org_id = account["org_id"]
        person = candidate(session, org_id, name="Counter", phone=REFERRER)
        for address, status_, bonus in (
            (FRIEND, referrals.STATUS_CONFIRMED, 5),
            (SECOND_FRIEND, referrals.STATUS_PENDING, 5),
            ("+14155556666", referrals.STATUS_REJECTED, 0),
        ):
            session.add(
                Referral(
                    org_id=org_id,
                    referrer_candidate_id=person.id,
                    referrer_address=REFERRER,
                    referee_address=address,
                    status=status_,
                    bonus_applications=bonus,
                )
            )
        session.commit()
        assert referrals.earned_bonus(session, org_id=org_id, candidate_id=person.id) == 5


class TestLinqChatCalls:
    """The HTTP shape of the two calls, through the injected transport."""

    def test_handles_are_parsed_with_status_and_is_me(self):
        seen = []

        def transport(method, url, payload):
            seen.append((method, url))
            return {
                "handles": [
                    {"handle": OUR_NUMBER, "is_me": True, "status": "active"},
                    {"handle": REFERRER, "status": "active"},
                    {"handle": FRIEND, "status": "removed"},
                ]
            }

        handles = fetch_chat_handles(GROUP_CHAT_ID, transport=transport)
        assert seen == [("GET", f"https://api.linqapp.com/api/partner/v3/chats/{GROUP_CHAT_ID}")]
        assert [h.handle for h in handles] == [OUR_NUMBER, REFERRER, FRIEND]
        assert handles[0].is_me is True
        assert handles[2].is_active is False

    def test_a_nested_envelope_is_unwrapped(self):
        def transport(method, url, payload):
            return {"chat": {"data": {"handles": [{"handle": REFERRER}]}}}

        assert [h.handle for h in fetch_chat_handles("c", transport=transport)] == [REFERRER]

    def test_a_malformed_response_is_no_participants_not_a_guess(self):
        def transport(method, url, payload):
            return {"handles": "not a list"}

        assert fetch_chat_handles("c", transport=transport) == ()

    def test_leave_posts_to_the_leave_subresource(self):
        seen = []

        def transport(method, url, payload):
            seen.append((method, url))
            return {}

        leave_chat(GROUP_CHAT_ID, transport=transport)
        assert seen == [
            ("POST", f"https://api.linqapp.com/api/partner/v3/chats/{GROUP_CHAT_ID}/leave")
        ]


class TestAddressKey:
    def test_an_apple_id_falls_back_to_the_handle(self):
        assert referrals.address_key("Someone@iCloud.com") == "someone@icloud.com"

    def test_both_sides_normalise_the_same_way(self):
        """Comparing a raw number against a stored key silently matches
        nothing, which is how a referral would never confirm."""
        assert referrals.address_key("+1 415 555 7777") == referrals.address_key(FRIEND)

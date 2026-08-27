"""`juicebox reset-thread`: being a stranger again.

The test that matters is not "the rows are gone" — it is that the next
message gets the intro. A thread that half-survives resumes mid-onboarding,
which is exactly the state that makes a test run tell you nothing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.cli import cmd_reset_thread
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import ACTION_ASKED, run_turn
from juicebox.messaging.linkedin_flow import FLOW_LINKEDIN_SHORT
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import (
    Candidate,
    CandidatePreference,
    IntakeMessage,
    IntakeThread,
    Suppression,
)

PHONE = "+16462010859"


class _Args:
    def __init__(self, **kw):
        self.phone = PHONE
        self.apply = False
        self.keep_candidate = False
        self.clear_suppression = False
        self.__dict__.update(kw)


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


def _seed(session, account, channel):
    """A thread part-way through onboarding, with a candidate attached."""
    from juicebox.messaging import identity

    thread = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-reset",
        entry_point="linq_inbound",
        from_address=PHONE,
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
    )
    record_inbound(session, thread=thread, body="hey", provider_native_id="z0")
    run_turn(session, thread=thread)
    record_inbound(session, thread=thread, body="sathya@example.com", provider_native_id="z1")
    run_turn(session, thread=thread)
    candidate_id = identity.link_candidate(session, thread=thread)
    session.add(
        CandidatePreference(
            org_id=account["org_id"],
            candidate_id=candidate_id,
            spec={"job_titles": ["investor"]},
        )
    )
    session.commit()
    return thread, candidate_id


def test_a_dry_run_deletes_nothing(session, account, channel, capsys):
    _seed(session, account, channel)
    assert cmd_reset_thread(_Args()) == 0

    out = capsys.readouterr().out
    assert "dry run" in out
    assert session.scalar(select(IntakeThread).where(IntakeThread.from_address == PHONE))


def test_apply_clears_the_conversation_and_the_candidate(
    session, account, channel, capsys
):
    _, candidate_id = _seed(session, account, channel)
    assert cmd_reset_thread(_Args(apply=True)) == 0

    session.expire_all()
    assert session.scalar(select(IntakeThread).where(IntakeThread.from_address == PHONE)) is None
    assert session.get(Candidate, candidate_id) is None
    assert session.scalar(select(CandidatePreference)) is None
    assert session.scalar(select(IntakeMessage)) is None


def test_the_next_message_gets_the_intro(session, account, channel):
    """The whole point. Not "rows are gone" -- "we are strangers"."""
    _seed(session, account, channel)
    cmd_reset_thread(_Args(apply=True))
    session.expire_all()
    channel.bodies.clear()

    fresh = open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-reset-2",
        entry_point="linq_inbound",
        from_address=PHONE,
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
    )
    record_inbound(session, thread=fresh, body="hey", provider_native_id="n0")
    result = run_turn(session, thread=fresh)

    assert result.action == ACTION_ASKED
    # The intro turn is two bubbles (greeting, then the profile ask); the
    # greeting is the first.
    intro = channel.bodies[0]
    # A reset really does start over: the full intro, not a mid-conversation
    # message. Asserted on the disclosures it still carries rather than on the
    # opt-out recital it no longer does (INTAKE_COMPLIANCE.md section 6a).
    assert "acme talent" in intro.lower()
    assert "ai" in intro.lower()
    # Back to the first ask too: the profile question rides the same turn.
    assert "resume" in channel.bodies[-1].lower() or "linkedin" in channel.bodies[-1].lower()


def test_keep_candidate_leaves_the_crm_record(session, account, channel):
    _, candidate_id = _seed(session, account, channel)
    cmd_reset_thread(_Args(apply=True, keep_candidate=True))

    session.expire_all()
    assert session.scalar(select(IntakeThread).where(IntakeThread.from_address == PHONE)) is None
    assert session.get(Candidate, candidate_id) is not None


def test_an_opt_out_survives_unless_you_say_otherwise(session, account, channel, capsys):
    """Everywhere else in this codebase a STOP is permanent. Clearing one is
    a decision the operator has to type out."""
    _seed(session, account, channel)
    session.add(
        Suppression(org_id=account["org_id"], phone=PHONE, reason="unsubscribe", email=None)
    )
    session.commit()

    cmd_reset_thread(_Args(apply=True))
    session.expire_all()
    assert session.scalar(select(Suppression).where(Suppression.phone == PHONE)) is not None

    cmd_reset_thread(_Args(apply=True, clear_suppression=True))
    session.expire_all()
    assert session.scalar(select(Suppression).where(Suppression.phone == PHONE)) is None


def test_a_suppression_can_be_cleared_after_the_thread_is_gone(
    session, account, channel
):
    """Reset first, then realise the STOP is why nothing sends.

    Clearing it must not require a thread to still exist -- the obvious order
    of operations deletes the thread first, and a command that then refuses to
    help is a trap.
    """
    _seed(session, account, channel)
    session.add(
        Suppression(org_id=account["org_id"], phone=PHONE, reason="unsubscribe", email=None)
    )
    session.commit()

    cmd_reset_thread(_Args(apply=True))
    session.expire_all()
    assert cmd_reset_thread(_Args(apply=True, clear_suppression=True)) == 0
    session.expire_all()
    assert session.scalar(select(Suppression).where(Suppression.phone == PHONE)) is None


def test_an_unknown_number_is_not_an_error(session, account, capsys):
    assert cmd_reset_thread(_Args(phone="+15550000000")) == 0
    assert "no intake thread" in capsys.readouterr().out

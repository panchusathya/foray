"""A thumbs-down on a job card, and why it is not the mirror image of a
thumbs-up.

The two directions cost different things when they are wrong, so they are
deliberately asymmetrical:

* **Yes** files an application, which cannot be unsent. It goes through a job,
  takes every rail, and only two unambiguous spellings count (`like`, `love`).
* **No** is a row update and sends nothing at all. One spelling counts
  (`dislike`), because nothing else reads unambiguously as a refusal, and the
  worst case is that a role drops out of a list the next batch refills.

Declining sends no reply on purpose: a confirmation bubble per dismissed role
is noise, and the candidate can already see their own tapback on the bubble.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from test_messaging_manager import STRONG, _managed, _posting, _say

from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.linq import parse_reaction
from juicebox.messaging.threads import open_thread
from juicebox.models import Application, Event, IntakeMessage, JobMatch, JobProposal


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    """A Linq thread, exactly as the webhook opens one."""
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-decline",
        entry_point="linq_inbound",
        from_address="+14155550199",
        consent_messaging=True,
    )


def _react(session, thread, native_id, reaction, *, from_me=False, event="reaction.added"):
    from juicebox.routers.messaging import _handle_linq_reaction

    return _handle_linq_reaction(
        session,
        {
            "event_type": event,
            "data": {
                "chat_id": thread.provider_thread_id,
                "message_id": native_id,
                "reaction_type": reaction,
                "is_from_me": from_me,
            },
        },
    )


def _present(session, account, thread, companies=("Rippling", "Figma")):
    thread = _managed(session, thread)
    org_id = account["org_id"]
    for index, company in enumerate(companies):
        posting = _posting(session, org_id, company=company, suffix=f"dn{index}")
        session.add(
            JobMatch(
                org_id=org_id,
                job_posting_id=posting.id,
                candidate_id=thread.candidate_id,
                score=0.9,
                criteria=[STRONG],
                status="new",
            )
        )
    session.flush()
    _say(session, thread, "find me roles", native_id="dn-present")
    proposals = list(
        session.scalars(select(JobProposal).order_by(JobProposal.created_at)).all()
    )
    assert len(proposals) >= 2
    return thread, proposals


class TestParsing:
    @pytest.mark.parametrize("reaction", ["like", "love"])
    def test_positive_spellings(self, reaction):
        event = parse_reaction(
            {
                "event_type": "reaction.added",
                "data": {"chat_id": "c", "message_id": "m", "reaction_type": reaction},
            }
        )
        assert event.is_positive is True
        assert event.is_negative is False

    def test_dislike_is_negative(self):
        event = parse_reaction(
            {
                "event_type": "reaction.added",
                "data": {"chat_id": "c", "message_id": "m", "reaction_type": "dislike"},
            }
        )
        assert event.is_negative is True
        assert event.is_positive is False

    @pytest.mark.parametrize("reaction", ["laugh", "emphasize", "question", "sticker"])
    def test_ambiguous_tapbacks_are_neither(self, reaction):
        """`question` is the one worth naming: it is a question, not a refusal,
        and reading it as one would drop a role somebody was asking about."""
        event = parse_reaction(
            {
                "event_type": "reaction.added",
                "data": {"chat_id": "c", "message_id": "m", "reaction_type": reaction},
            }
        )
        assert event.is_positive is False
        assert event.is_negative is False

    @pytest.mark.parametrize("reaction", ["like", "dislike"])
    def test_a_removal_is_not_an_instruction(self, reaction):
        """Taking a thumb back is the absence of a decision, not a new one.

        Reading a removed thumbs-up as a decline -- or a removed thumbs-down as
        renewed interest -- would invent an intent out of somebody undoing a
        mis-tap.
        """
        event = parse_reaction(
            {
                "event_type": "reaction.removed",
                "data": {"chat_id": "c", "message_id": "m", "reaction_type": reaction},
            }
        )
        assert event.is_positive is False
        assert event.is_negative is False

    @pytest.mark.parametrize("reaction", ["like", "dislike"])
    def test_our_own_tapback_is_not_a_signal(self, reaction):
        """The agent tapbacks its own acknowledgements. Acting on those would
        make it answer itself."""
        event = parse_reaction(
            {
                "event_type": "reaction.added",
                "data": {
                    "chat_id": "c",
                    "message_id": "m",
                    "reaction_type": reaction,
                    "is_from_me": True,
                },
            }
        )
        assert event.is_positive is False
        assert event.is_negative is False


class TestDeclining:
    def test_a_dislike_declines_that_one_role(self, session, account, channel, thread):
        thread, proposals = _present(session, account, thread)
        target, other = proposals[0], proposals[1]
        card = session.get(IntakeMessage, target.message_id)
        before = len(channel.sent)

        result = _react(session, thread, card.provider_native_id, "dislike")

        assert result["acted"] is True
        assert result["declined"] is True
        assert result["proposal_id"] == target.id
        session.refresh(target)
        assert target.status == "declined"
        assert target.decline_reason == "tapback:dislike"
        assert target.responded_at is not None
        # The card that was tapped, since a tapback is not an IntakeMessage.
        assert target.responded_message_id == target.message_id
        # Only that one.
        session.refresh(other)
        assert other.status == "proposed"

        # Nothing was filed and nothing was said.
        assert session.scalar(select(Application)) is None
        assert len(channel.sent) == before

    def test_it_records_an_event(self, session, account, channel, thread):
        """The recruiter side needs to know a role was actively refused, which
        is different information from a role never answered."""
        thread, proposals = _present(session, account, thread)
        card = session.get(IntakeMessage, proposals[0].message_id)
        _react(session, thread, card.provider_native_id, "dislike")

        event = session.scalar(
            select(Event).where(Event.type == "proposal.declined")
        )
        assert event is not None
        assert event.subject_id == proposals[0].id
        assert event.payload["reason"] == "tapback:dislike"
        assert event.payload["job_posting_id"] == proposals[0].job_posting_id

    def test_a_declined_role_is_not_proposed_again(
        self, session, account, channel, thread
    ):
        """The decline is what has to make it stick, and it does so through
        `eligible_matches`, which skips any posting this candidate already has
        a proposal for whatever its status."""
        from juicebox.messaging.manager import eligible_matches

        thread, proposals = _present(session, account, thread)
        card = session.get(IntakeMessage, proposals[0].message_id)
        _react(session, thread, card.provider_native_id, "dislike")

        remaining = eligible_matches(
            session, org_id=account["org_id"], candidate_id=thread.candidate_id
        )
        declined_posting = proposals[0].job_posting_id
        assert declined_posting not in [posting.id for _, posting in remaining]

    def test_a_second_delivery_of_the_same_tapback_is_a_no_op(
        self, session, account, channel, thread
    ):
        """Webhooks are always retried. The status guard is the idempotency."""
        thread, proposals = _present(session, account, thread)
        card = session.get(IntakeMessage, proposals[0].message_id)
        first = _react(session, thread, card.provider_native_id, "dislike")
        second = _react(session, thread, card.provider_native_id, "dislike")

        assert first["acted"] is True
        assert second["acted"] is False
        assert second["reason"] == "already_declined"
        events = session.scalars(
            select(Event).where(Event.type == "proposal.declined")
        ).all()
        assert len(events) == 1

    def test_a_dislike_after_a_yes_does_not_rewrite_it(
        self, session, account, channel, thread
    ):
        """An accepted proposal has an application hanging off it, possibly
        already submitted. Flipping the proposal to declined would leave that
        application attached to a role the record says was refused; the honest
        answer to a change of mind is a conversation, not a status rewrite.
        """
        thread, proposals = _present(session, account, thread)
        target = proposals[0]
        target.status = "accepted"
        target.responded_at = datetime.now(tz=UTC)
        session.flush()
        card = session.get(IntakeMessage, target.message_id)

        result = _react(session, thread, card.provider_native_id, "dislike")

        assert result["acted"] is False
        assert result["reason"] == "already_accepted"
        session.refresh(target)
        assert target.status == "accepted"
        assert target.decline_reason == ""

    def test_a_dislike_on_a_non_card_message_is_ignored(
        self, session, account, channel, thread
    ):
        """The tail bubble carries no proposal. A thumbs-down on it means
        something, but not something we can act on, so it is not a decline of
        whatever role happens to be nearest."""
        thread, proposals = _present(session, account, thread)
        tail = session.scalars(
            select(IntakeMessage)
            .where(IntakeMessage.thread_id == thread.id)
            .order_by(IntakeMessage.seq.desc())
        ).first()
        assert tail is not None
        result = _react(session, thread, tail.provider_native_id, "dislike")
        assert result["acted"] is False
        assert result.get("reason") == "not_a_job_card"
        for proposal in proposals:
            session.refresh(proposal)
            assert proposal.status == "proposed"

    @pytest.mark.parametrize("reaction", ["laugh", "emphasize", "question"])
    def test_an_ambiguous_tapback_on_a_card_changes_nothing(
        self, session, account, channel, thread, reaction
    ):
        thread, proposals = _present(session, account, thread)
        card = session.get(IntakeMessage, proposals[0].message_id)
        result = _react(session, thread, card.provider_native_id, reaction)
        assert result["acted"] is False
        session.refresh(proposals[0])
        assert proposals[0].status == "proposed"
        assert session.scalar(select(Application)) is None

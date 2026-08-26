"""What a delivery receipt, an opt-out, or a carrier error must not break.

Five of the bugs this suite pins down were one bug: ``IntakeMessage.status``
was carrying two facts at once. ``record_outbound`` writes ``sent`` to mean
"the channel took it"; ``apply_status_event`` then overwrites the same column
with ``delivered`` / ``read`` when Linq's receipts arrive, and the deployment
subscribes to exactly those events. Every reader that asked ``status == "sent"``
therefore got the right answer for a few seconds and the wrong one forever
after -- the transcript lost our own questions, the LinkedIn intro re-sent on
every reply, and the nudge budget could never be spent.

The rest are the safety rails: an apply that arrives below ``run_turn`` still
has to pass the stopped-thread check, an opt-out is honoured whatever state the
thread is in, a card the carrier never delivered must not bury the posting, and
an inbound text must not join another tenant's conversation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from juicebox.applications.limits import APPLICATIONS_PER_COMPANY_PER_30D
from juicebox.intake.schema import coverage, empty_record
from juicebox.messaging import cadence, identity
from juicebox.messaging.channels import LINQ, WEB, FakeChannel, set_channel
from juicebox.messaging.conversation import (
    ACTION_STOPPED,
    _reread,
    run_turn,
)
from juicebox.messaging.manager import (
    MODE_MANAGING,
    apply_to_proposal,
    eligible_matches,
    present_matches,
)
from juicebox.messaging.suppression import address_has_opted_out, handle_stop
from juicebox.messaging.threads import (
    HANDED_OFF,
    STATUS_COMPLETE,
    STATUS_STOPPED,
    apply_status_event,
    close_thread,
    find_active_thread,
    messages_of,
    open_thread,
    record_inbound,
    transcript_of,
    was_handed_off,
)
from juicebox.models import (
    Application,
    IntakeMessage,
    JobMatch,
    JobPosting,
    JobProposal,
    Org,
)

# Winter, so the unknown-timezone window (11:00-21:00 America/New_York) is
# 16:00Z-02:00Z and 08:00Z is the middle of the night.
OUT_OF_WINDOW = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)

STRONG = {
    "name": "title", "label": "Title", "kind": "title", "weight": 1.0,
    "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": [],
}


@pytest.fixture
def linq_channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def web_thread(session, account, fake_channel):
    return open_thread(
        session, org_id=account["org_id"], channel=WEB, consent_messaging=True
    )


@pytest.fixture
def linq_thread(session, account, linq_channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-receipts",
        entry_point="linq_inbound",
        from_address="+14155550100",
        consent_messaging=True,
    )


def _say(session, thread, body, *, native_id=""):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _receipts_for(session, thread, event_type="message.delivered"):
    """Every outbound row on the thread gets the receipt Linq would send."""
    stamped = 0
    for message in messages_of(session, thread):
        if not message.provider_native_id or message.direction != "outbound":
            continue
        apply_status_event(
            session,
            provider_native_id=message.provider_native_id,
            event_type=event_type,
            delivered_at=datetime.now(UTC),
        )
        stamped += 1
    assert stamped, "no outbound rows carried a provider id to stamp"
    return stamped


def _core_complete_record() -> dict:
    record = empty_record()
    record["spec"].update(
        {
            "job_titles": ["machine learning engineer"],
            "seniority": ["senior"],
            "locations": ["san francisco"],
            "remote_ok": False,
        }
    )
    record["target_base"] = 240000.0
    record["comp_currency"] = "USD"
    record["timeline"] = "under_1_month"
    record["work_authorization"] = "green card"
    record["ok_to_share_with_clients"] = True
    return record


def _managed(session, thread):
    thread.contact_name = "Ada Okonkwo"
    thread.contact_email = "ada-receipts@example.com"
    thread.record = _core_complete_record()
    thread.mode = MODE_MANAGING
    identity.link_candidate(session, thread=thread)
    session.flush()
    return thread


def _posting(session, org_id, *, company="Anthropic", title="ML Engineer", suffix="1"):
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"receipts-{company.lower()}-{suffix}",
        title=title,
        company=company,
        company_normalized=company.lower(),
        jd_text="Build ML systems at scale.",
        status="open",
    )
    session.add(posting)
    session.flush()
    return posting


def _match(session, org_id, posting, candidate_id, *, score=0.9):
    row = JobMatch(
        org_id=org_id,
        job_posting_id=posting.id,
        candidate_id=candidate_id,
        score=score,
        criteria=[STRONG],
        status="new",
    )
    session.add(row)
    session.flush()
    return row


# --------------------------------------------------------------------------
# the status column carries one fact, not two
# --------------------------------------------------------------------------


class TestHandoffPredicate:
    def test_every_receipt_state_still_counts_as_handed_off(self):
        assert set(HANDED_OFF) == {"sent", "delivered", "read"}

    def test_a_failed_row_is_not_handed_off(self, session, web_thread, fake_channel):
        fake_channel._configured = False
        run_turn(session, thread=web_thread)
        rows = [m for m in messages_of(session, web_thread) if m.direction == "outbound"]
        assert rows and all(not was_handed_off(m) for m in rows)


class TestTranscriptSurvivesReceipts:
    def test_a_delivered_question_stays_in_the_transcript(
        self, session, web_thread, fake_channel
    ):
        """Bug: the receipt erased the question underneath its own answer.

        We ask about comp, the receipt lands, the candidate answers "260", and
        the transcript handed to the extractor was a bare ``Candidate: 260``
        with nothing to bind the number to.
        """
        run_turn(session, thread=web_thread)
        before = transcript_of(session, web_thread)
        assert "Agent:" in before

        _receipts_for(session, web_thread)

        assert transcript_of(session, web_thread) == before

    def test_a_read_receipt_does_not_erase_it_either(
        self, session, web_thread, fake_channel
    ):
        run_turn(session, thread=web_thread)
        before = transcript_of(session, web_thread)
        _receipts_for(session, web_thread, event_type="message.read")
        assert transcript_of(session, web_thread) == before

    def test_a_failed_message_is_still_left_out(self, session, web_thread, fake_channel):
        """The other half of the rule: what nobody received is not conversation."""
        run_turn(session, thread=web_thread)
        for message in messages_of(session, web_thread):
            if message.direction == "outbound":
                message.status = "failed"
        session.flush()
        assert "Agent:" not in transcript_of(session, web_thread)

    def test_the_answer_still_reaches_the_record_after_receipts(
        self, session, web_thread, fake_channel
    ):
        """End to end: receipts arrive between every turn and the conversation
        still moves forward rather than re-asking what it just asked."""
        run_turn(session, thread=web_thread)
        _receipts_for(session, web_thread)
        _say(session, web_thread, "Ada Okonkwo", native_id="r-1")
        _receipts_for(session, web_thread)
        _say(session, web_thread, "ada-receipts@example.com", native_id="r-2")
        _receipts_for(session, web_thread)

        assert web_thread.contact_name == "Ada Okonkwo"
        assert web_thread.contact_email == "ada-receipts@example.com"
        # Every question we asked is still readable as ours.
        assert transcript_of(session, web_thread).count("Agent:") >= 3


class TestLinkedInIntroSendsOnce:
    def test_a_delivered_intro_is_not_sent_again(
        self, session, linq_thread, linq_channel
    ):
        """Bug: ``outbound_sent`` tested ``status == "sent"``, so once the
        receipt stamped the intro ``delivered`` it read as never sent -- and a
        candidate who replied "who is this?" without an email got the whole
        greeting again, on that reply and on every one after it."""
        _say(session, linq_thread, "hey", native_id="i-1")
        intro = linq_channel.bodies[-1]
        assert intro

        _receipts_for(session, linq_thread)
        _say(session, linq_thread, "who is this?", native_id="i-2")

        assert linq_channel.bodies.count(intro) == 1

    def test_and_not_on_the_third_reply_either(
        self, session, linq_thread, linq_channel
    ):
        _say(session, linq_thread, "hey", native_id="j-1")
        intro = linq_channel.bodies[-1]
        for index, body in enumerate(("who is this?", "sorry, who?", "??")):
            _receipts_for(session, linq_thread)
            _say(session, linq_thread, body, native_id=f"j-{index + 2}")
        assert linq_channel.bodies.count(intro) == 1


class TestNudgeBudgetSurvivesReceipts:
    def _nudge(self, session, thread, body, *, native_id):
        from juicebox.messaging.channels import DeliveryResult, OutboundMessage
        from juicebox.messaging.threads import record_outbound

        return record_outbound(
            session,
            thread=thread,
            message=OutboundMessage(body=body),
            result=DeliveryResult(status="sent", provider_native_id=native_id),
            origin=cadence.ORIGIN_NUDGE,
        )

    def test_a_delivered_nudge_still_charges_the_budget(
        self, session, linq_thread, linq_channel, monkeypatch
    ):
        """Latent today (nothing passes ``kind=INITIATIVE_NUDGE`` in prod) and
        a spam loop the moment something does: delivered nudges dropped out of
        the count, so every nudge gate reopened."""
        now = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
        linq_thread.last_inbound_at = now - timedelta(days=30)
        for index in range(cadence.MAX_NUDGES_PER_THREAD):
            row = self._nudge(session, linq_thread, f"still there? {index}", native_id=f"n-{index}")
            row.sent_at = now - timedelta(days=10 - index)
        session.flush()

        _receipts_for(session, linq_thread)

        decision = cadence.may_initiate(
            session, thread=linq_thread, now=now, kind=cadence.INITIATIVE_NUDGE
        )
        assert decision.allowed is False
        assert decision.reason == cadence.REASON_NUDGE_BUDGET

    def test_a_failed_nudge_is_not_charged(
        self, session, linq_thread, linq_channel
    ):
        """The budget charges for what was sent, not what was attempted."""
        now = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
        linq_thread.last_inbound_at = now - timedelta(days=30)
        for index in range(cadence.MAX_NUDGES_PER_THREAD):
            row = self._nudge(session, linq_thread, f"nudge {index}", native_id=f"f-{index}")
            row.status = "failed"
        session.flush()

        decision = cadence.may_initiate(
            session, thread=linq_thread, now=now, kind=cadence.INITIATIVE_NUDGE
        )
        assert decision.reason != cadence.REASON_NUDGE_BUDGET


# --------------------------------------------------------------------------
# rails that used to be skipped
# --------------------------------------------------------------------------


class TestTapbackRespectsTheRails:
    def _proposed(self, session, account, thread, channel):
        thread = _managed(session, thread)
        posting = _posting(session, account["org_id"], suffix="tapback")
        _match(session, account["org_id"], posting, thread.candidate_id)
        _say(session, thread, "find me roles", native_id="t-1")
        proposal = session.scalar(select(JobProposal))
        assert proposal is not None
        return thread, proposal

    def test_a_tapback_after_stop_applies_to_nothing_and_texts_nobody(
        self, session, account, linq_thread, linq_channel
    ):
        """Bug: the worker's ``apply_proposal_id`` branch returns before
        ``run_turn``, so a candidate who texted STOP could scroll up two days
        later, thumbs-up last week's card, and get an Application plus an
        "on it" text on a do-not-contact number."""
        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)
        handle_stop(session, thread=thread)
        before = len(linq_channel.sent)

        result = apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        assert result.get("skipped")
        assert session.scalar(select(Application)) is None
        assert len(linq_channel.sent) == before

    def test_a_tapback_from_a_suppressed_candidate_stops_the_thread(
        self, session, account, linq_thread, linq_channel
    ):
        from juicebox.outreach.suppressions import suppress

        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)
        suppress(
            session,
            org_id=thread.org_id,
            email=thread.contact_email,
            reason="unsubscribe",
            source="test",
        )
        before = len(linq_channel.sent)

        result = apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        assert result.get("skipped") == "do-not-contact"
        assert thread.status == STATUS_STOPPED
        assert session.scalar(select(Application)) is None
        assert len(linq_channel.sent) == before

    def test_a_tapback_in_quiet_hours_is_deferred_not_dropped(
        self, session, account, linq_thread, linq_channel, monkeypatch
    ):
        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)
        thread.last_inbound_at = OUT_OF_WINDOW - timedelta(hours=6)
        session.flush()
        monkeypatch.setattr(cadence, "utcnow", lambda: OUT_OF_WINDOW)
        before = len(linq_channel.sent)

        result = apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        assert result.get("defer") is True
        assert result.get("defer_until") is not None
        assert len(linq_channel.sent) == before

    def test_an_ordinary_tapback_still_applies(
        self, session, account, linq_thread, linq_channel
    ):
        """The rails must not swallow the happy path."""
        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)

        result = apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        assert result.get("action")
        assert session.scalar(select(Application)) is not None

    def test_a_stopped_thread_gets_no_read_receipt_and_no_typing_bubble(
        self, session, account, linq_thread, linq_channel, monkeypatch
    ):
        """The rails inside ``apply_to_proposal`` stop the application and the
        text, but the worker sends a read receipt and a "typing..." bubble
        before it ever reaches them -- both of which are contact with somebody
        who texted STOP."""
        from juicebox.jobs import KIND_INTAKE_TURN, enqueue
        from juicebox.messaging import linq as linq_module
        from juicebox.worker import process_job

        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)
        handle_stop(session, thread=thread)

        touched: list[str] = []
        monkeypatch.setattr(
            linq_module, "maybe_mark_read", lambda t: touched.append("read")
        )
        monkeypatch.setattr(
            linq_module, "maybe_start_typing", lambda t: touched.append("typing")
        )

        job = enqueue(
            session,
            org_id=thread.org_id,
            kind=KIND_INTAKE_TURN,
            idempotency_key=f"turn:{thread.id}:tapback-after-stop",
            payload={"thread_id": thread.id, "apply_proposal_id": proposal.id},
        )
        job.status = "running"
        process_job(session, job)

        assert touched == []
        assert session.scalar(select(Application)) is None

    def test_an_active_thread_still_gets_its_receipt(
        self, session, account, linq_thread, linq_channel, monkeypatch
    ):
        from juicebox.jobs import KIND_INTAKE_TURN, enqueue
        from juicebox.messaging import linq as linq_module
        from juicebox.worker import process_job

        thread, proposal = self._proposed(session, account, linq_thread, linq_channel)

        touched: list[str] = []
        monkeypatch.setattr(
            linq_module, "maybe_mark_read", lambda t: touched.append("read")
        )
        monkeypatch.setattr(
            linq_module, "maybe_start_typing", lambda t: touched.append("typing")
        )

        job = enqueue(
            session,
            org_id=thread.org_id,
            kind=KIND_INTAKE_TURN,
            idempotency_key=f"turn:{thread.id}:tapback",
            payload={"thread_id": thread.id, "apply_proposal_id": proposal.id},
        )
        job.status = "running"
        process_job(session, job)

        assert touched == ["read", "typing"]


class TestStopIsHonouredAtAnyState:
    def test_stop_on_a_completed_web_thread_opts_them_out(
        self, session, web_thread, fake_channel
    ):
        """Bug: the ``STATUS_COMPLETE`` early return sat above the STOP branch,
        so a visitor who typed STOP after the wrap-up got no suppression row,
        no stopped thread and no acknowledgement -- and the same visitor id was
        greeted again on the next session."""
        web_thread.from_address = "visitor-stop-1"
        close_thread(session, thread=web_thread, status=STATUS_COMPLETE)

        result = _say(session, web_thread, "STOP", native_id="s-1")

        assert result.action == ACTION_STOPPED
        assert web_thread.status == STATUS_STOPPED
        assert address_has_opted_out(
            session, channel=WEB, from_address="visitor-stop-1"
        )

    def test_a_completed_thread_that_says_anything_else_stays_silent(
        self, session, web_thread, fake_channel
    ):
        """The early return still does its job for every other message."""
        close_thread(session, thread=web_thread, status=STATUS_COMPLETE)
        before = len(fake_channel.sent)
        result = _say(session, web_thread, "one more thing", native_id="s-2")
        assert result.action == "completed"
        assert len(fake_channel.sent) == before


# --------------------------------------------------------------------------
# proposals and postings
# --------------------------------------------------------------------------


class TestUndeliveredCardsDoNotBuryThePosting:
    def test_a_card_the_carrier_refused_leaves_the_posting_presentable(
        self, session, account, linq_thread, linq_channel
    ):
        """Bug: the proposal row was written before the send and kept whatever
        happened, and ``eligible_matches`` treats any proposal as answered --
        so a carrier 500 meant "Nothing new clears the bar right now" for that
        role permanently."""
        thread = _managed(session, linq_thread)
        posting = _posting(session, account["org_id"], suffix="dead-carrier")
        _match(session, account["org_id"], posting, thread.candidate_id)
        linq_channel._configured = False

        present_matches(session, thread=thread, state=coverage(thread.record))

        assert session.scalars(select(JobProposal)).all() == []
        again = eligible_matches(
            session, org_id=thread.org_id, candidate_id=thread.candidate_id
        )
        assert [p.id for _, p in again] == [posting.id]

    def test_the_frame_is_not_overwritten_with_cards_nobody_saw(
        self, session, account, linq_thread, linq_channel
    ):
        """``last_presented`` is what "apply to 1" resolves against."""
        thread = _managed(session, linq_thread)
        seen = [{"proposal_id": "jprop_old", "posting_id": "jp_old",
                 "company": "Old", "title": "Old Role"}]
        thread.last_presented = seen
        posting = _posting(session, account["org_id"], suffix="unseen")
        _match(session, account["org_id"], posting, thread.candidate_id)
        linq_channel._configured = False

        present_matches(session, thread=thread, state=coverage(thread.record))

        assert thread.last_presented == seen

    def test_a_delivered_card_is_still_frozen_on_the_frame(
        self, session, account, linq_thread, linq_channel
    ):
        thread = _managed(session, linq_thread)
        posting = _posting(session, account["org_id"], suffix="alive")
        _match(session, account["org_id"], posting, thread.candidate_id)

        present_matches(session, thread=thread, state=coverage(thread.record))

        assert [e["posting_id"] for e in thread.last_presented] == [posting.id]
        proposal = session.scalar(select(JobProposal))
        assert proposal is not None and proposal.message_id


class TestProposalAcceptedOnlyOnceTheApplicationExists:
    def _at_the_company_cap(self, session, org_id, thread):
        """Fill the per-company 30-day budget so the next apply is refused."""
        for index in range(APPLICATIONS_PER_COMPANY_PER_30D):
            other = _posting(session, org_id, suffix=f"cap-{index}")
            session.add(
                Application(
                    org_id=org_id,
                    candidate_id=thread.candidate_id,
                    job_posting_id=other.id,
                    source="candidate",
                    status="draft",
                )
            )
        session.flush()

    def test_a_refused_application_leaves_the_proposal_proposed(
        self, session, account, linq_thread, linq_channel
    ):
        """Bug: ``status = "accepted"`` was set unconditionally ahead of
        ``create_application``, so a refusal left a proposal reading as
        agreed-and-handled with nothing queued and no retry path."""
        thread = _managed(session, linq_thread)
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="capped")
        _match(session, org_id, posting, thread.candidate_id)
        _say(session, thread, "find me roles", native_id="c-1")
        proposal = session.scalar(select(JobProposal))
        self._at_the_company_cap(session, org_id, thread)

        apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        session.refresh(proposal)
        assert proposal.status == "proposed"
        assert proposal.responded_at is None

    def test_the_parked_copy_names_the_limit_that_actually_fired(
        self, session, account, linq_thread, linq_channel
    ):
        """It used to say "this week's application limit" -- that is the
        weekly submission cap, a different rule the candidate has not hit."""
        thread = _managed(session, linq_thread)
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="copy")
        _match(session, org_id, posting, thread.candidate_id)
        _say(session, thread, "find me roles", native_id="c-2")
        proposal = session.scalar(select(JobProposal))
        self._at_the_company_cap(session, org_id, thread)

        apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        said = linq_channel.bodies[-1].lower()
        assert "week" not in said
        assert "month" in said

    def test_a_successful_apply_still_accepts_the_proposal(
        self, session, account, linq_thread, linq_channel
    ):
        thread = _managed(session, linq_thread)
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="accepts")
        _match(session, org_id, posting, thread.candidate_id)
        _say(session, thread, "find me roles", native_id="c-3")
        proposal = session.scalar(select(JobProposal))

        apply_to_proposal(session, thread=thread, proposal_id=proposal.id)

        session.refresh(proposal)
        assert proposal.status == "accepted"
        assert proposal.responded_at is not None


# --------------------------------------------------------------------------
# review notes and tenancy
# --------------------------------------------------------------------------


class TestRereadKeepsTheIdentityConflict:
    def test_a_duplicate_identity_note_survives_a_clean_turn(
        self, session, web_thread, fake_channel
    ):
        """Bug: ``_reread`` assigned ``review_needed`` and overwrote
        ``review_reason``, so one clean turn dropped the thread off the review
        queue with a live duplicate-identity conflict nobody would ever see."""
        web_thread.contact_name = "Ada Okonkwo"
        web_thread.contact_email = "ada-dupe@example.com"
        web_thread.record = _core_complete_record()
        web_thread.review_needed = True
        web_thread.review_reason = identity.join_reasons(
            "", identity.CONFLICT_REASON_PREFIX + "ada-dupe@example.com"
        )
        record_inbound(session, thread=web_thread, body="senior ML in SF", provider_native_id="d-1")

        _reread(session, thread=web_thread)

        assert web_thread.review_needed is True
        assert identity.has_identity_conflict(web_thread)

    def test_the_same_extractor_reason_is_not_appended_twice(self):
        joined = identity.join_reasons("already looked at", "already looked at")
        assert joined == "already looked at"

    def test_an_ordinary_reason_is_still_recorded(self):
        joined = identity.join_reasons("first", "second")
        assert joined == "first; second"


class TestThreadLookupIsScopedToTheOrg:
    def _second_org(self, session):
        # Built in this session rather than through signup: the fixture session
        # holds SQLite's write lock, and a second connection trying to insert
        # would simply block.
        org = Org(name="Beta Talent", slug="beta-talent")
        session.add(org)
        session.flush()
        return org.id

    def test_another_tenants_active_thread_is_not_returned(
        self, session, account, linq_channel
    ):
        """Bug: ``find_active_thread`` filtered on channel, address and status
        only. A candidate with an active thread at org A who texted org B's
        number landed back in A's thread -- the message was written under A,
        the reply was composed from A's transcript and delivered on B's line,
        and B's recruiters never saw it."""
        number = "+14155550777"
        theirs = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-org-a",
            from_address=number,
            consent_messaging=True,
        )
        other_org = self._second_org(session)

        assert (
            find_active_thread(
                session, org_id=other_org, channel=LINQ, from_address=number
            )
            is None
        )
        assert (
            find_active_thread(
                session, org_id=account["org_id"], channel=LINQ, from_address=number
            ).id
            == theirs.id
        )

    def test_the_owning_org_still_resumes_its_own_thread(
        self, session, account, linq_thread
    ):
        found = find_active_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            from_address=linq_thread.from_address,
        )
        assert found is not None and found.id == linq_thread.id


class TestReceiptsDoNotChangeWhatWasRecorded:
    def test_a_receipt_never_rewrites_a_failed_row(self, session, web_thread, fake_channel):
        """A carrier that reports delivery for a message we never handed over
        would be a contradiction; ``failed`` wins."""
        fake_channel._configured = False
        run_turn(session, thread=web_thread)
        row = session.scalar(
            select(IntakeMessage).where(IntakeMessage.thread_id == web_thread.id)
        )
        row.provider_native_id = "late-receipt"
        session.flush()

        apply_status_event(
            session,
            provider_native_id="late-receipt",
            event_type="message.delivered",
            delivered_at=datetime.now(UTC),
        )
        assert row.status == "failed"
        assert not was_handed_off(row)

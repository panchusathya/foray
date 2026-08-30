"""A stated budget, a ranked gate, and the two ways a caller becomes a record.

Four things that all failed on the same demo call and are easiest to hold
together: the caller said "thirty seconds" and answered eight questions; the
questions came in the order they happen to be declared in; the address he spelt
out was read off the transcript as nothing at all; and the number he rang from
never reached his record.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from juicebox.intake.contact import scan_transcript
from juicebox.intake.schema import CORE_KEYS, GATE_PRIORITY, coverage
from juicebox.intake.service import (
    apply_budget,
    elapsed_since_budget,
    UNVERIFIED_NUMBER_REASON,
    drop_reason,
    elapsed_seconds,
    gate_keys_for,
    link_caller,
)
from juicebox.messaging.channels import LINQ
from juicebox.messaging.threads import open_thread
from juicebox.messaging.voice_handoff import MODE_VOICE_VERIFY, confirm_text_verification
from juicebox.models import Candidate, CandidatePhone, IntakeCall, utcnow
from juicebox.repository import sync_candidate_emails, sync_candidate_phones

OUR_NUMBER = "+16283865454"
CALLER = "+12134317465"
EMAIL = "tushar31gupta@gmail.com"


@pytest.fixture
def linq_on(monkeypatch):
    """Opening a verification thread needs the text channel configured."""
    monkeypatch.setenv("LINQ_ENABLED", "true")
    monkeypatch.setenv("LINQ_API_KEY", "lk_test")
    monkeypatch.setenv("LINQ_FROM_NUMBER", "+14155550100")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


# --------------------------------------------------------------------------
# the budget buys a number of questions
# --------------------------------------------------------------------------


class TestTheBudgetIsSized:
    """"Thirty seconds" has to mean something other than "four questions"."""

    def test_under_a_minute_buys_two_questions(self):
        assert gate_keys_for(0) == GATE_PRIORITY[:2]

    def test_a_minute_buys_four(self):
        assert gate_keys_for(1) == GATE_PRIORITY[:4]

    def test_the_core_threshold_still_caps_a_short_budget(self):
        """Sizing was added underneath the core rule, not in place of it.

        Arithmetic alone gives three minutes twelve questions -- more than the
        four a three-minute caller got before any of this existed. A stated
        budget must never widen the gate.
        """
        for minutes in (2, 3, 4):
            assert len(gate_keys_for(minutes) or ()) == len(CORE_KEYS)

    def test_a_long_budget_is_the_full_set(self):
        assert gate_keys_for(5) is None
        assert gate_keys_for(20) is None

    def test_no_budget_is_the_full_set(self):
        assert gate_keys_for(None) is None

    def test_a_long_answer_spends_the_budget(self):
        """The clock is real where the stated total is only a prediction."""
        assert len(gate_keys_for(2, elapsed_seconds=90) or ()) == 2

    def test_an_overrun_call_still_gets_one_question(self):
        """Never an empty gate: a call that ran over closes, it is not cut off."""
        assert gate_keys_for(0, elapsed_seconds=600) == GATE_PRIORITY[:1]


class TestOptionalTopicsStopWhenTheTimeIsGone:
    """The nice-to-have question is the one a short caller must not be asked."""

    def test_a_spent_budget_closes_the_optional_list(self):
        from juicebox.intake.service import has_room_for_another_question

        assert has_room_for_another_question(0, 20) is False
        assert has_room_for_another_question(1, 55) is False

    def test_time_left_still_allows_one(self):
        from juicebox.intake.service import has_room_for_another_question

        assert has_room_for_another_question(0, 0) is True
        assert has_room_for_another_question(5, 0) is True

    def test_no_stated_budget_is_never_cut_off(self):
        """Only a caller who told us their limit is held to it."""
        from juicebox.intake.service import has_room_for_another_question

        assert has_room_for_another_question(None, 10_000) is True

    def test_the_snapshot_drops_optional_topics_when_asked_to(self):
        from juicebox.intake import empty_record
        from juicebox.intake.schema import coverage
        from juicebox.intake.service import _snapshot

        filled = empty_record()
        filled["spec"].update(
            {
                "job_titles": ["senior frontend engineer"],
                "seniority": ["senior"],
                "locations": ["san francisco"],
                "remote_ok": False,
                "must_have_skills": ["react"],
            }
        )
        filled["target_base"] = 200000.0
        filled["work_authorization"] = "authorized"
        filled["timeline"] = "now"
        filled["employment_status"] = "unemployed_searching"
        filled["active_processes"] = "none"
        filled["ok_to_share_with_clients"] = True
        state = coverage(filled)
        assert state.required_complete

        with_room = _snapshot(state, covered_count=len(state.covered), record=filled)
        without = _snapshot(
            state, covered_count=len(state.covered), record=filled, allow_optional=False
        )
        # Wrapping up is unaffected -- only the extra ask goes away.
        assert with_room["wrap_up"] is True and without["wrap_up"] is True
        assert without["optional_topics"] == []


class TestTheGateACallClosedOnIsRecoverable:
    """The match pitch asks after the fact, and must not re-derive from the clock."""

    def test_no_trim_means_the_full_set(self):
        from juicebox.intake.service import effective_gate_keys

        assert effective_gate_keys(IntakeCall(gate_mode="full", deferred_fields=[])) is None

    def test_a_short_call_is_judged_on_what_it_asked(self):
        """Otherwise the pitch refuses exactly the callers it is most useful for.

        A thirty-second caller closes on two questions. Measured against the
        core set instead, the intake reads as incomplete and no roles are
        offered -- to the person with the least patience for a follow-up call.
        """
        from juicebox.intake.service import effective_gate_keys

        call = IntakeCall(
            gate_mode="core",
            deferred_fields=[
                "compensation",
                "spec.remote_ok",
                "work_authorization",
                "spec.seniority",
                "spec.must_have_skills",
                "timeline",
                "employment_status",
                "active_processes",
                "ok_to_share_with_clients",
            ],
        )
        assert effective_gate_keys(call) == ("spec.job_titles", "spec.locations")

    def test_a_core_call_with_nothing_deferred_reads_as_it_always_did(self):
        from juicebox.intake.service import effective_gate_keys

        gate = effective_gate_keys(IntakeCall(gate_mode="core", deferred_fields=[]))
        assert set(gate or ()) == set(CORE_KEYS)


class TestElapsedIsOnlyTrustedWhenPlausible:
    def test_no_start_timestamp_reads_as_zero(self):
        assert elapsed_seconds(None) == 0
        assert elapsed_seconds(IntakeCall(started_at=None)) == 0

    def test_a_stale_timestamp_is_refused(self):
        """A payload can carry a timestamp from last year.

        Believing it would silently shrink a full intake to its single most
        valuable question -- the quietest possible way to stop asking people
        things -- so anything past a call's maximum length reads as zero.
        """
        old = IntakeCall(started_at=utcnow() - timedelta(days=400))
        assert elapsed_seconds(old) == 0

    def test_a_live_call_is_measured(self):
        live = IntakeCall(started_at=utcnow() - timedelta(seconds=45))
        assert 40 <= elapsed_seconds(live) <= 60


class TestTheBudgetClockStartsWhenTheBudgetIsStated:
    """The bug that ended a call without asking anything.

    A caller said "just a minute" about fifty seconds in -- after the greeting,
    the recording question, the role question and spelling out an email. The
    gate charged all fifty against the sixty they had just offered, found ten
    seconds left, and ten seconds buys zero exchanges. So the gate shrank to its
    single highest-priority question, that question was already answered, and
    the agent thanked them and wrapped up without asking a thing.

    A budget is a promise about the future. Everything before it was our time.
    """

    def test_the_anchor_is_stamped_with_the_budget(self):
        call = IntakeCall(started_at=utcnow() - timedelta(seconds=50))
        assert call.budget_started_at is None
        apply_budget(call, 1)
        assert call.budget_started_at is not None

    def test_no_budget_stamps_no_anchor(self):
        call = IntakeCall(started_at=utcnow() - timedelta(seconds=50))
        apply_budget(call, None)
        assert call.budget_started_at is None

    def test_the_anchor_latches_like_the_budget_does(self):
        """A second reading must not restart the clock they are spending."""
        call = IntakeCall(started_at=utcnow())
        apply_budget(call, 1)
        first = call.budget_started_at
        apply_budget(call, 5)
        assert call.budget_started_at == first
        assert call.minutes_available == 1

    def test_time_before_the_budget_is_not_charged_against_it(self):
        """The regression itself, in the arithmetic that produced it."""
        call = IntakeCall(started_at=utcnow() - timedelta(seconds=50))
        apply_budget(call, 1)
        assert elapsed_since_budget(call) < 5
        # The whole minute is still theirs, so the gate is the core set rather
        # than the single question fifty spent seconds would have left.
        assert gate_keys_for(1, elapsed_since_budget(call)) == GATE_PRIORITY[: len(CORE_KEYS)]

    def test_time_after_the_budget_still_is(self):
        """The sizing this was always meant to do is untouched."""
        call = IntakeCall(started_at=utcnow() - timedelta(seconds=120))
        apply_budget(call, 1)
        call.budget_started_at = utcnow() - timedelta(seconds=50)
        assert 45 <= elapsed_since_budget(call) <= 60
        assert len(gate_keys_for(1, elapsed_since_budget(call))) == 1

    def test_a_call_with_no_anchor_reads_as_zero(self):
        """Rows written before the column existed size on the stated total."""
        assert elapsed_since_budget(None) == 0
        assert elapsed_since_budget(IntakeCall(budget_started_at=None)) == 0

    def test_a_stale_anchor_is_refused_like_a_stale_start(self):
        old = IntakeCall(budget_started_at=utcnow() - timedelta(days=400))
        assert elapsed_since_budget(old) == 0


# --------------------------------------------------------------------------
# the questions come back in a useful order
# --------------------------------------------------------------------------


class TestTheGateIsRanked:
    def test_the_ranking_covers_every_required_field(self):
        """Pinned here as well as asserted at import, so the failure names itself."""
        required = {k for k in GATE_PRIORITY}
        assert len(required) == len(GATE_PRIORITY)

    def test_what_they_want_is_asked_before_anything_else(self):
        missing = coverage({}).required_missing
        assert missing[0] == "spec.job_titles"
        assert missing[1] == "spec.locations"

    def test_work_authorisation_waits_its_turn(self):
        """It led on the demo call, before the caller had said what job he wanted.

        Declaration order is authoring order and means nothing to a caller.
        """
        missing = coverage({}).required_missing
        assert missing.index("work_authorization") > missing.index("compensation")

    def test_an_answered_field_drops_out_of_the_ranking(self):
        missing = coverage({"spec": {"job_titles": ["senior frontend engineer"]}}).required_missing
        assert "spec.job_titles" not in missing
        assert missing[0] == "spec.locations"


# --------------------------------------------------------------------------
# reading the address off the call
# --------------------------------------------------------------------------


DEMO_CALL = """Agent: Frontend engineering, good. Can I get your name and email to start your record?
User: Yeah. My name is Tushar, and you know it's tushar thirty one gabta at g mail dot com.
Agent: Let me make sure I've got that -- t-u-s-h-a-r-3-1-g-a-b-t-a at gmail dot com, is that right?
User: No. It's no. It t u s h a r three one g u p t a at g mail dot com.
Agent: Got it, thanks for the correction -- t-u-s-h-a-r-3-1-g-u-p-t-a at gmail dot com.
"""


class TestASpelledAddressIsRead:
    def test_the_demo_call_yields_the_corrected_address(self):
        """The whole exchange, exactly as Retell transcribed it.

        Every part of this was previously lost: the name (the answer opened
        "Yeah." and the filler stripper stopped at the full stop), the first
        address (spoken with a number word), and the correction (the agent's
        read-back carried no cue word, so the turn answering it was not read).
        """
        assert scan_transcript(DEMO_CALL) == ("Tushar", EMAIL)

    def test_a_spelled_local_part_is_not_read_short(self):
        """"t u s h a r at gmail dot com" used to decode as ``r@gmail.com``.

        A single letter is a lead-in word, so the guard that exists to stop a
        local part being read one word short accepted the last letter of a
        spelling as the whole address -- putting a stranger's mailbox on a
        candidate record, which is the failure that module exists to prevent.
        """
        from juicebox.intake.contact import decode_spoken_email

        assert decode_spoken_email("it is t u s h a r at gmail dot com") == "tushar@gmail.com"

    def test_an_ambiguous_local_part_is_still_refused(self):
        """Two plain words with no separator could be either. Refusing is right."""
        from juicebox.intake.contact import decode_spoken_email

        assert decode_spoken_email("it is priya raman at gmail dot com") == ""

    def test_prose_that_merely_contains_at_and_a_suffix_is_refused(self):
        from juicebox.intake.contact import decode_spoken_email

        assert decode_spoken_email("we are hosted at aws dot com") == ""

    def test_a_hyphenated_name_survives_the_spelling_collapse(self):
        from juicebox.intake.contact import decode_spoken_email

        assert decode_spoken_email("it is mary-jane at gmail dot com") == "mary-jane@gmail.com"

    def test_the_second_real_call_yields_name_and_corrected_address(self):
        """The 6:23pm call, as Retell transcribed it. Three prior losses in one:

        the name arrived as "Tushar and then <the email>" with no comma, so the
        batch splitter never cut it and the four-word cap threw it away; the
        correction opened "And then t no. It's actually ..." -- connective
        filler and a transcription stray before the cue; and "actually" sat to
        the left of the address, which the lead-in list refused.
        """
        call = (
            "Agent: Got it, front-end. Can I get your name and email so I can"
            " attach this call to your record?\n"
            "User: Yeah. Tushar and then Tushar thirty one at gmail dot com.\n"
            "Agent: Let me make sure I have that -- t-u-s-h-a-r-3-1 at gmail"
            " dot com?\n"
            "User: And then t no. It's actually t u s h a r three one"
            " g u p t a at Gmail dot com.\n"
            "Agent: Got it -- tushar31gupta at gmail dot com. Thanks, Tushar.\n"
        )
        assert scan_transcript(call) == ("Tushar", EMAIL)

    def test_a_mid_sentence_no_is_not_a_correction(self):
        """The filler allowance is bounded: a buried "no" must not reopen an
        address that was read back and confirmed."""
        from juicebox.intake.contact import _CORRECTION

        assert not _CORRECTION.match("I think the answer is no on that role")

    def test_a_confirmed_read_back_beats_the_garbled_caller_turn(self):
        """The third real call: ASR heard "Ishara ... gabta", the agent read
        back the right address, the caller said yes. The scanner reads caller
        turns only, so without adopting the confirmed spelling it stores the
        garbled mailbox -- a wrong address ratified by a call that got it right.
        """
        call = (
            "Agent: Got it. Can I get your email address?\n"
            "User: Ishara thirty one gabta at g mail dot com.\n"
            "Agent: Let me make sure I have that -- t-u-s-h-a-r-3-1-g-u-p-t-a"
            " at gmail dot com?\n"
            "User: Yes.\n"
        )
        assert scan_transcript(call) == ("", EMAIL)

    def test_a_rejected_read_back_is_not_adopted(self):
        """"No" to the spelling means the read-back was wrong: the correction
        in the same turn wins, never the spelling the caller just refused."""
        call = (
            "Agent: Can I get your name and email to start your record?\n"
            "User: Yeah. My name is Tushar, it's tushar thirty one gabta at"
            " g mail dot com.\n"
            "Agent: Let me make sure -- t-u-s-h-a-r-3-1-g-a-b-t-a at gmail"
            " dot com, is that right?\n"
            "User: No. It's t u s h a r three one g u p t a at g mail dot com.\n"
        )
        assert scan_transcript(call) == ("Tushar", EMAIL)


# --------------------------------------------------------------------------
# the number reaches the record only once something proves it
# --------------------------------------------------------------------------


def _call(session, org_id, **overrides):
    values = {
        "org_id": org_id,
        "provider": "retell",
        "provider_call_id": "call_budget_1",
        "entry_point": "dial_in",
        "from_number": CALLER,
        "to_number": OUR_NUMBER,
        "contact_name": "Tushar",
        "contact_email": EMAIL,
        "extracted": {"spec": {"job_titles": ["senior frontend engineer"]}},
    }
    values.update(overrides)
    call = IntakeCall(**values)
    session.add(call)
    session.flush()
    return call


def _numbers(session, candidate_id):
    return [
        row.phone
        for row in session.scalars(
            select(CandidatePhone).where(CandidatePhone.candidate_id == candidate_id)
        )
    ]


class TestAMatchByEmailClaimsTheNumber:
    """Changed 2026-08-29, deliberately, with the trade written down.

    The number is what a handset claims and ANI is spoofable, so this is not
    proof. It is accepted anyway, on the same reasoning ``resolve_candidate_id``
    already accepted it on the read side: the dial-in line holds what a person
    said they want from a job, not an account anybody can spend, and requiring a
    recruiter to promote every number by hand meant no caller was ever
    recognised twice in practice.

    What is NOT accepted is a number moving between people -- see
    ``test_a_number_somebody_else_owns_is_still_refused``.
    """

    def test_the_call_attaches_and_so_does_the_number(self, session, account):
        person = Candidate(
            org_id=account["org_id"], source="test", source_id="t1",
            full_name="Tushar Gupta", emails=[EMAIL], phones=[],
        )
        session.add(person)
        session.flush()
        sync_candidate_emails(session, person)

        call = _call(session, account["org_id"])
        link_caller(session, call)

        assert call.candidate_id == person.id
        assert _numbers(session, person.id) != [], "the caller is still a stranger next time"
        assert UNVERIFIED_NUMBER_REASON not in call.review_reason

    def test_a_number_somebody_else_owns_is_still_refused(self, session, account):
        """The bound on the damage. First claim wins, and the clash is surfaced.

        A shared handset is the likely way this goes wrong -- a partner's
        phone, a desk line, a recycled number -- and it needs nobody's bad
        intent. Moving the number to whoever rang most recently would make the
        dial-in line greet each of them as the other.
        """
        first = Candidate(
            org_id=account["org_id"], source="test", source_id="t1",
            full_name="Priya Raman", emails=["priya@example.com"], phones=[CALLER],
        )
        session.add(first)
        session.flush()
        sync_candidate_emails(session, first)
        sync_candidate_phones(session, first)

        second = Candidate(
            org_id=account["org_id"], source="test", source_id="t2",
            full_name="Tushar Gupta", emails=[EMAIL], phones=[],
        )
        session.add(second)
        session.flush()
        sync_candidate_emails(session, second)

        call = _call(session, account["org_id"])
        link_caller(session, call)

        assert call.candidate_id == second.id
        assert _numbers(session, second.id) == [], "a claimed number was moved"
        assert _numbers(session, first.id) != []
        assert call.review_needed is True
        assert "already owned by somebody else" in call.review_reason


class TestAReplyPromotesTheNumber:
    def test_confirming_the_text_writes_the_number_and_clears_the_note(
        self, session, account, linq_on
    ):
        """A reply is the assertion caller ID cannot make.

        Whoever holds the handset sent a message from it, inbound -- the same
        evidence ``attribute_call`` waits for a recruiter to supply by hand.
        """
        person = Candidate(
            org_id=account["org_id"], source="test", source_id="t2",
            full_name="Tushar Gupta", emails=[EMAIL], phones=[],
        )
        session.add(person)
        session.flush()
        sync_candidate_emails(session, person)

        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat_budget_1",
            from_address=CALLER,
            mode=MODE_VOICE_VERIFY,
            consent_messaging=False,
        )
        call = _call(session, account["org_id"])
        link_caller(session, call)
        # Since 2026-08-29 the number is already on the row by this point. The
        # reply path still has to be idempotent rather than redundant: it also
        # runs for callers link_caller could not attribute at all, and writing
        # a row that exists must not raise or duplicate.
        assert _numbers(session, person.id) == [CALLER]

        call.text_verification_thread_id = thread.id
        call.text_verification_status = "sent"
        call.text_verification_sent_at = utcnow()
        call.matches_offered = [{"posting_id": "jpost_1", "line": "a role", "score": 0.9}]
        session.flush()

        confirm_text_verification(session, thread=thread)

        assert _numbers(session, person.id) == [CALLER]
        assert UNVERIFIED_NUMBER_REASON not in call.review_reason

    def test_an_unattributed_call_has_nothing_to_write_to(self, session, account, linq_on):
        """No candidate means no row. A number alone never creates one."""
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat_budget_2",
            from_address=CALLER,
            mode=MODE_VOICE_VERIFY,
            consent_messaging=False,
        )
        call = _call(
            session,
            account["org_id"],
            provider_call_id="call_budget_2",
            contact_email="",
            text_verification_thread_id=thread.id,
            text_verification_status="sent",
            text_verification_sent_at=utcnow(),
        )

        confirm_text_verification(session, thread=thread)

        assert call.candidate_id is None
        assert session.scalars(select(Candidate)).all() == []


class TestDropReason:
    def test_it_removes_only_the_named_reason(self):
        joined = f"something else; {UNVERIFIED_NUMBER_REASON}; a third thing"
        assert drop_reason(joined, UNVERIFIED_NUMBER_REASON) == "something else; a third thing"

    def test_removing_the_only_reason_empties_it(self):
        assert drop_reason(UNVERIFIED_NUMBER_REASON, UNVERIFIED_NUMBER_REASON) == ""

    def test_a_reason_that_is_not_there_changes_nothing(self):
        assert drop_reason("something else", UNVERIFIED_NUMBER_REASON) == "something else"

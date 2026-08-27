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
from juicebox.repository import sync_candidate_emails

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


class TestAMatchByEmailWithholdsTheNumber:
    def test_the_call_attaches_but_the_number_does_not(self, session, account):
        """Caller ID is what a handset claims, and ANI is spoofable.

        The record is theirs -- they spoke the address -- but "somebody who
        knows this address rang from this phone" is not "this is their phone".
        """
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
        assert _numbers(session, person.id) == []
        assert UNVERIFIED_NUMBER_REASON in call.review_reason
        assert call.review_needed is True


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
        assert _numbers(session, person.id) == []

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

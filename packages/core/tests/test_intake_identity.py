"""Who called: the caller's own name and address, and what they authorise.

The inbound agent has always asked a caller for their name and the email they
use with us -- "so a recruiter can find them". Nothing stored the answer, so a
call from a number that was not already in ``candidate_phones`` produced a
completed intake that no person was attached to, and a human had to retype an
address that was sitting in the transcript.

The rules that matter here are all about how far a stated identity is allowed to
reach:

**An address identifies; a number does not.** Caller ID is spoofable, so an
address the caller spoke may attribute the record to its owner, but must not
write the calling number onto that person's row -- the dial-in line reads comp
and pipeline off a phone lookup.

**A name is not an identity.** There are four Priya Ramans.

**A stranger still does not enter the CRM for free.** Creating anybody needs a
completed intake, which is the same price the text channel charges.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.intake.contact import (
    decode_spoken_email,
    email_in_turn,
    scan_transcript,
)
from juicebox.intake.extract import extract_intake
from juicebox.intake.service import (
    UNIDENTIFIED_REASON,
    UNVERIFIED_NUMBER_REASON,
    attribute_call,
    counterparty_number,
    extract_call,
    link_caller,
    record_contact,
)
from juicebox.llm.client import LLMClient, set_llm
from juicebox.models import Candidate, CandidateEmail, CandidatePhone, IntakeCall
from juicebox.repository import sync_candidate_emails, sync_candidate_phones

CALLER = "+14155550123"
OUR_NUMBER = "+16283865454"


@pytest.fixture
def no_llm():
    """Force the heuristic path. Identity has to work with no API key."""
    set_llm(LLMClient(api_key="", transport=None, enabled=False))
    yield
    set_llm(None)


# --------------------------------------------------------------------------
# spoken addresses
# --------------------------------------------------------------------------


class TestSpokenEmail:
    """An address arrives spoken on a call, so the written regex finds nothing."""

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("priya dot raman at gmail dot com", "priya.raman@gmail.com"),
            ("priya at gmail dot com", "priya@gmail.com"),
            ("p dot raman at fastmail dot co dot uk", "p.raman@fastmail.co.uk"),
            ("priya underscore raman at gmail dot com", "priya_raman@gmail.com"),
            ("priya dash raman at gmail dot com", "priya-raman@gmail.com"),
        ],
    )
    def test_a_spoken_address_is_decoded(self, said, expected):
        assert decode_spoken_email(said) == expected

    def test_a_lead_in_does_not_glue_onto_the_local_part(self):
        """The bug a span-based decoder has: "it's priya..." -> "spriya@..."."""
        assert decode_spoken_email("it's priya dot raman at gmail dot com") == (
            "priya.raman@gmail.com"
        )

    def test_a_double_at_reads_the_real_address(self):
        """"reach me at X at Y" -- the first "at" is English, the second is @.

        Without the known-suffix rule the local part gets read as a domain and
        this comes out as "me@priya.raman".
        """
        assert decode_spoken_email("reach me at priya dot raman at gmail dot com") == (
            "priya.raman@gmail.com"
        )

    def test_prose_is_not_an_address(self):
        """"we're hosted at aws dot com" is a sentence, not a mailbox."""
        assert email_in_turn("we're hosted at aws dot com") == ""
        assert email_in_turn("I worked at IBM dot com for years") == ""

    def test_the_cue_is_what_admits_a_spoken_address(self):
        """Same words; only one turn is about an address."""
        said = "priya dot raman at gmail dot com"
        assert email_in_turn(said) == ""
        assert email_in_turn(f"my email is {said}") == said.replace(" dot ", ".").replace(
            " at ", "@"
        )
        assert email_in_turn(said, cued=True) == "priya.raman@gmail.com"

    @pytest.mark.parametrize(
        ("said", "expected"),
        [
            ("my email is dashawn at gmail dot com", "dashawn@gmail.com"),
            ("my email is point72 at gmail dot com", "point72@gmail.com"),
            ("my email is jdotson at gmail dot com", "jdotson@gmail.com"),
            ("email me at pointer at gmail dot com", "pointer@gmail.com"),
        ],
    )
    def test_a_separator_word_inside_a_word_is_not_a_separator(self, said, expected):
        """These four came back as "is-awn@", "is.72@", "j.son@" and "at.er@".

        The separator alternation was spliced in with no word boundaries, so any
        name containing "dash", "dot" or "point" was torn open mid-word and the
        leading ``\\s*`` reached back and swallowed the word before it. Each
        wreck still satisfied the address regex, so it was returned rather than
        rejected -- and became a CRM row, and then mail, under a stranger's
        mailbox.
        """
        assert decode_spoken_email(said) == expected

    def test_an_ambiguous_local_part_is_refused_not_guessed(self):
        """"priya raman at gmail dot com" has no separator between the names.

        The pattern can only match from "raman", and "raman@gmail.com" is not a
        near miss -- it is somebody else's mailbox. There is no way to tell
        whether the speaker meant "priyaraman" or "priya.raman", so nothing is
        the only safe answer.
        """
        assert decode_spoken_email("priya raman at gmail dot com") == ""


class TestTranscriptScan:
    TRANSCRIPT = (
        "Agent: Hi, this is Vivian on Foray's candidate line. This call is "
        "recorded so a recruiter can read it later, is that OK?\n"
        "Candidate: Yes, fine.\n"
        "Agent: Can I take your name and the email you use with Foray?\n"
        "Candidate: It's Priya Raman, and the email is priya dot raman at gmail dot com.\n"
        "Agent: Thanks. What are you looking for next?\n"
        "Candidate: A staff backend role in New York.\n"
    )

    def test_both_details_come_off_the_transcript(self):
        assert scan_transcript(self.TRANSCRIPT) == (
            "Priya Raman",
            "priya.raman@gmail.com",
        )

    def test_our_own_words_are_never_read_as_the_caller(self):
        """The failure the text channel already recorded, on the voice side.

        The heuristic extractor has no notion of who is speaking, and given a
        whole transcript it read a job title out of the agent's greeting. A
        name is only ever taken from the answer to the name question.
        """
        transcript = (
            "Agent: Hi, this is Vivian, an AI recruiting assistant with Foray.\n"
            "Candidate: I'm looking for staff backend roles.\n"
        )
        name, email = scan_transcript(transcript)
        assert name == "", "the agent's own introduction became a candidate name"
        assert email == ""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("Sure, it's Dashawn Miller, and my email is d at gmail dot com", "Dashawn Miller"),
            ("Yes, Priya Raman", "Priya Raman"),
            ("Of course, Priya Raman", "Priya Raman"),
            ("Hi, it's Priya", "Priya"),
            ("Hiro Tanaka", "Hiro Tanaka"),
        ],
    )
    def test_the_filler_before_a_name_is_not_the_name(self, answer, expected):
        """People acknowledge the question before answering it.

        Only "hi|hey|hello" was stripped, so these were recorded as callers
        named Sure, Yes and Of course -- and the next call opened "Hi Sure --
        it's Vivian at Foray."
        """
        transcript = f"Agent: Can I take your name?\nCandidate: {answer}\n"
        assert scan_transcript(transcript)[0] == expected

    @pytest.mark.parametrize(
        "answer",
        ["Sure.", "Yes, that's right", "No problem, one moment", "Okay, hold on"],
    )
    def test_an_answer_with_no_name_in_it_yields_no_name(self, answer):
        """Nothing is the right answer: the agent asks again, and an easy
        question repeated costs less than a wrong name on a client profile."""
        transcript = f"Agent: Can I take your name?\nCandidate: {answer}\n"
        assert scan_transcript(transcript)[0] == ""


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


class TestExtractionCarriesTheContact:

    def test_the_contact_is_not_in_the_record(self, no_llm):
        """It would be a spec key ``jobmatch.engine`` cannot score."""
        result = extract_intake(TestTranscriptScan.TRANSCRIPT)
        assert "contact_name" not in result.record
        assert "contact_email" not in result.record
        assert "contact_name" not in result.record["spec"]

    def test_identity_does_not_close_the_coverage_gate(self, no_llm):
        """A caller who gives a name has not answered a hiring question."""
        result = extract_intake(
            "Agent: What is your name?\nCandidate: Priya Raman.\n"
        )
        assert result.contact_name == "Priya Raman"
        assert not result.coverage.required_complete

    def test_a_model_sentence_is_not_a_name(self, monkeypatch):
        """From a live call: the model answered this field with an explanation.

        Stored as-is it would have gone onto a CRM row as somebody's name.
        """
        from types import SimpleNamespace

        import juicebox.intake.extract as mod

        class FakeLLM:
            available = True

            def json(self, **kwargs):
                return SimpleNamespace(
                    data={
                        "contact_name": "the caller did not give a name",
                        "contact_email": "unknown",
                    }
                )

            json_fast = json

        monkeypatch.setattr(mod, "get_llm", lambda: FakeLLM())
        result = mod.extract_intake("Candidate: hello", known="")
        assert result.contact_name == ""
        assert result.contact_email == ""


# --------------------------------------------------------------------------
# linking
# --------------------------------------------------------------------------


# Enough of a record to count as "the call captured something" -- the guard
# ``link_caller`` applies before creating anybody.
def a_record(**overrides):
    from juicebox.intake.schema import empty_record

    record = empty_record()
    record["employment_status"] = "employed_searching"
    record.update(overrides)
    return record


def a_call(session, account, **overrides):
    fields = dict(
        org_id=account["org_id"],
        provider="retell",
        provider_call_id="call_identity_1",
        entry_point="inbound",
        from_number=CALLER,
        to_number=OUR_NUMBER,
        status="in_progress",
        extracted=a_record(),
    )
    fields.update(overrides)
    call = IntakeCall(**fields)
    session.add(call)
    session.flush()
    return call


def a_candidate(session, account, *, name, email="", phone=""):
    candidate = Candidate(
        org_id=account["org_id"],
        source="test",
        source_id=f"test-{name}-{email}-{phone}",
        full_name=name,
        emails=[email] if email else [],
        phones=[phone] if phone else [],
    )
    session.add(candidate)
    session.flush()
    sync_candidate_emails(session, candidate)
    sync_candidate_phones(session, candidate)
    return candidate


class TestLinkCaller:
    def test_an_address_we_know_attributes_the_call(self, session, account):
        owner = a_candidate(
            session, account, name="Priya Raman", email="priya.raman@gmail.com"
        )
        call = a_call(session, account, contact_email="priya.raman@gmail.com")

        assert link_caller(session, call) == owner.id
        assert call.candidate_id == owner.id

    def test_an_email_match_claims_the_number(self, session, account):
        """Changed 2026-08-29. The number now rides on the address.

        The caller spoke this address, on this call, from this number. That is
        not proof -- ANI is spoofable -- but it is the same evidence the
        attribution itself rests on, and withholding the number meant the
        dial-in line never recognised anybody a second time.
        """
        owner = a_candidate(
            session, account, name="Priya Raman", email="priya.raman@gmail.com"
        )
        call = a_call(session, account, contact_email="priya.raman@gmail.com")
        link_caller(session, call)

        keys = set(
            session.scalars(
                select(CandidatePhone.phone).where(CandidatePhone.candidate_id == owner.id)
            )
        )
        assert CALLER in keys, "the caller will be a stranger on their next call"
        assert UNVERIFIED_NUMBER_REASON not in call.review_reason

    def test_a_name_alone_creates_nobody(self, session, account):
        call = a_call(session, account, contact_name="Priya Raman")
        assert link_caller(session, call) is None
        assert call.candidate_id is None
        assert not session.scalars(
            select(Candidate).where(Candidate.full_name == "Priya Raman")
        ).all()

    def test_the_created_row_does_own_the_number(self, session, account):
        """This row exists only because this caller described themselves."""
        call = a_call(
            session,
            account,
            contact_name="Priya Raman",
            contact_email="priya.raman@gmail.com",
        )
        candidate_id = link_caller(session, call)
        keys = set(
            session.scalars(
                select(CandidatePhone.phone).where(
                    CandidatePhone.candidate_id == candidate_id
                )
            )
        )
        assert CALLER in keys

    def test_a_call_that_captured_nothing_creates_nobody(self, session, account):
        """A stranger does not enter the CRM for one sentence.

        A wrong number that ends "sorry -- oh, I'm Priya, priya@example.com"
        is not an intake.
        """
        call = a_call(
            session,
            account,
            contact_email="priya.raman@gmail.com",
            extracted={},
        )
        assert link_caller(session, call) is None
        assert not session.scalars(
            select(CandidateEmail).where(CandidateEmail.email == "priya.raman@gmail.com")
        ).all()

    def test_a_do_not_contact_address_creates_nobody(self, session, account):
        from juicebox.outreach.suppressions import suppress

        suppress(
            session,
            org_id=account["org_id"],
            email="priya.raman@gmail.com",
            reason="unsubscribe",
        )
        call = a_call(session, account, contact_email="priya.raman@gmail.com")
        assert link_caller(session, call) is None
        assert "do-not-contact" in call.review_reason

    def test_an_already_attributed_call_is_left_alone(self, session, account):
        known = a_candidate(session, account, name="Someone Else", phone=CALLER)
        other = a_candidate(
            session, account, name="Priya Raman", email="priya.raman@gmail.com"
        )
        call = a_call(
            session,
            account,
            candidate_id=known.id,
            contact_email="priya.raman@gmail.com",
        )
        assert link_caller(session, call) == known.id
        assert call.candidate_id != other.id


class TestRecordContact:
    def test_a_later_quieter_pass_does_not_blank_the_answer(self, session, account):
        """Latched like consent: a replay must not erase what a pass captured."""
        from juicebox.intake.extract import ExtractionResult

        call = a_call(session, account)
        record_contact(
            call,
            ExtractionResult(
                record={},
                extractor="llm",
                contact_name="Priya Raman",
                contact_email="priya.raman@gmail.com",
            ),
        )
        record_contact(
            call, ExtractionResult(record={}, extractor="llm")
        )
        assert call.contact_name == "Priya Raman"
        assert call.contact_email == "priya.raman@gmail.com"


class TestReviewReasons:
    def test_no_reason_contains_the_separator(self):
        """A reason holding "; " splits into two and stops matching itself.

        That is how the first version of ``_clear_identity_review`` left every
        attributed call on the review queue: the constant it was looking for
        could not survive the split it was looking through.
        """
        from juicebox.intake.service import _IDENTITY_REASONS

        for reason in _IDENTITY_REASONS:
            assert "; " not in reason, reason

    def test_a_mid_call_note_is_not_wiped_when_the_call_ends(
        self, session, account, monkeypatch
    ):
        """``coverage_snapshot`` writes onto this row *while* the call runs.

        Extraction assigned over the top of it, so a three-minute core-mode call
        that deferred work_authorization, timeline and employment_status ended
        with no trace of either note as soon as the model came back clean. Those
        notes are what make "how often does this happen?" a query instead of a
        guess.
        """
        from types import SimpleNamespace

        from intake_fixtures import COMPLETE_PAYLOAD, COMPLETE_TRANSCRIPT

        import juicebox.intake.extract as mod

        class FakeLLM:
            available = True
            voice_timeout = 3.5

            def json(self, **kwargs):
                return SimpleNamespace(data=dict(COMPLETE_PAYLOAD))

            json_fast = json

        monkeypatch.setattr(mod, "get_llm", lambda: FakeLLM())
        call = a_call(
            session,
            account,
            provider_call_id="call_identity_3",
            transcript=COMPLETE_TRANSCRIPT,
            review_needed=True,
            review_reason=(
                "extractor unavailable, gate ran on heuristics; "
                "core-mode trim, deferred fields unasked"
            ),
        )
        extract_call(session, call)

        assert "extractor unavailable, gate ran on heuristics" in call.review_reason
        assert "core-mode trim, deferred fields unasked" in call.review_reason
        assert call.review_needed


class TestCounterpartyNumber:

    def test_outbound_is_who_we_dialled(self, session, account):
        """``from_number`` is our own intake line on an outbound call.

        Reading it as the candidate's puts our number in candidate_phones,
        pointing the dial-in caller lookup at whoever last had a row written.
        """
        call = a_call(
            session,
            account,
            entry_point="outbound",
            from_number=OUR_NUMBER,
            to_number=CALLER,
        )
        assert counterparty_number(call) == CALLER


class TestAttribution:
    def test_a_human_vouching_promotes_the_number(self, session, account):
        """The assertion caller ID cannot make."""
        owner = a_candidate(
            session, account, name="Priya Raman", email="priya.raman@gmail.com"
        )
        call = a_call(session, account, contact_email="priya.raman@gmail.com")
        link_caller(session, call)

        # Attribution is now a confirmation rather than the only route: the
        # number went on at link time. It must still be idempotent, because a
        # recruiter working the queue will attribute calls that already match.
        attribute_call(session, call=call, candidate_id=owner.id)
        keys = set(
            session.scalars(
                select(CandidatePhone.phone).where(CandidatePhone.candidate_id == owner.id)
            )
        )
        assert CALLER in keys
        assert not call.review_needed
        assert call.review_reason == ""

    def test_an_address_somebody_else_owns_is_not_moved(self, session, account):
        a_candidate(session, account, name="Priya Raman", email="priya@example.com")
        target = a_candidate(session, account, name="Someone Else")
        call = a_call(session, account, contact_email="priya@example.com")

        attribute_call(session, call=call, candidate_id=target.id)
        assert "priya@example.com" not in (target.emails or [])
        assert call.review_needed, "a clash between two rows needs a human"

    def test_clearing_the_review_keeps_unrelated_reasons(self, session, account):
        """This used to be an equality check on one string.

        Once a call could carry two reasons, the check silently stopped
        matching and every attributed call stayed on the queue forever.
        """
        target = a_candidate(session, account, name="Priya Raman")
        call = a_call(
            session,
            account,
            review_needed=True,
            review_reason=f"{UNIDENTIFIED_REASON}; heuristic extraction",
        )
        attribute_call(session, call=call, candidate_id=target.id)
        assert call.review_reason == "heuristic extraction"
        assert call.review_needed, "an unrelated reason still needs a look"


class TestEndToEnd:
    def test_a_cold_call_that_introduces_itself_lands_on_a_person(
        self, session, account, no_llm
    ):
        """The whole point: no retyping an address that was in the transcript."""
        call = a_call(
            session,
            account,
            transcript=(
                "Agent: This call is recorded, is that OK?\n"
                "Candidate: Yes.\n"
                "Agent: Can I take your name and the email you use with Foray?\n"
                "Candidate: Priya Raman, and it's priya dot raman at gmail dot com.\n"
                "Agent: What are you looking for?\n"
                "Candidate: A senior machine learning engineer role in San Francisco. "
                "I'm actively looking, I'd want 240 base, within a month, I'm a green "
                "card holder so no sponsorship, and I'm not interviewing anywhere else.\n"
            ),
        )
        extract_call(session, call)

        assert call.contact_name == "Priya Raman"
        assert call.contact_email == "priya.raman@gmail.com"
        assert call.candidate_id, "a caller who introduced themselves stayed unattributed"
        candidate = session.get(Candidate, call.candidate_id)
        assert "priya.raman@gmail.com" in candidate.emails

    def test_a_silent_caller_still_produces_no_person(self, session, account, no_llm):
        """``AGENTS.md``: cold inbound calls must not fill the CRM with strangers."""
        call = a_call(
            session,
            account,
            provider_call_id="call_identity_2",
            transcript=(
                "Agent: This call is recorded, is that OK?\n"
                "Candidate: Yeah whatever.\n"
                "Agent: What are you looking for?\n"
                "Candidate: Not saying.\n"
            ),
        )
        extract_call(session, call)
        assert call.candidate_id is None
        assert call.review_needed
        assert UNIDENTIFIED_REASON in call.review_reason

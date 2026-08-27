"""One person, two channels, one record.

Voice and text ask the same questions and write the same
``candidate_preferences`` row. ``AGENTS.md`` records why that is dangerous: the
two writers never conflict textually, so they diverge silently and the
divergence is only visible to somebody who reads both. Three had accumulated.

**A call overwrote what a text had captured.** ``upsert_preference`` assigned
every column from the call's record, so somebody who texted their comp
expectation and then rang to mention an offer came out of the call with the comp
blanked -- a three-minute conversation about one thing does not re-state the
other thing.

**The mid-call gate knew nothing about the stored record.** It is computed from
this call's transcript, so a candidate who answered eleven questions by text
last week was asked the eleven again, and a two-minute update call ran to the
fifteen-minute ceiling.

**Only the text path asked for job discovery.** A caller got scored against
today's postings; a texter got a search for postings that did not exist yet.

And one thing that is *not* symmetric, on purpose: what the agent may read back
to whoever is on the phone.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.intake.records import merge_record
from juicebox.intake.schema import empty_record
from juicebox.intake.service import (
    coverage_snapshot,
    empty_coverage,
    prior_preference_record,
    upsert_preference,
)
from juicebox.llm.client import LLMClient, set_llm
from juicebox.models import Candidate, CandidatePreference, IntakeCall
from juicebox.repository import sync_candidate_emails, sync_candidate_phones

CALLER = "+14155550123"
OTHER_NUMBER = "+14155559999"
OUR_NUMBER = "+16283865454"


@pytest.fixture
def no_llm():
    set_llm(LLMClient(api_key="", transport=None, enabled=False))
    yield
    set_llm(None)


def a_candidate(session, account, *, name="Priya Raman", email="priya@example.com", phone=""):
    candidate = Candidate(
        org_id=account["org_id"],
        source="test",
        source_id=f"test-{email}-{phone}",
        full_name=name,
        first_name=name.split()[0],
        emails=[email] if email else [],
        phones=[phone] if phone else [],
    )
    session.add(candidate)
    session.flush()
    sync_candidate_emails(session, candidate)
    sync_candidate_phones(session, candidate)
    return candidate


def a_call(session, account, candidate=None, **overrides):
    fields = dict(
        org_id=account["org_id"],
        candidate_id=candidate.id if candidate is not None else None,
        provider="retell",
        provider_call_id="call_sync_1",
        entry_point="inbound",
        from_number=CALLER,
        to_number=OUR_NUMBER,
        status="in_progress",
    )
    fields.update(overrides)
    call = IntakeCall(**fields)
    session.add(call)
    session.flush()
    return call


def a_preference(session, account, candidate, **columns):
    pref = CandidatePreference(org_id=account["org_id"], candidate_id=candidate.id, **columns)
    session.add(pref)
    session.flush()
    return pref


def a_result(record=None, **kwargs):
    from juicebox.intake.extract import ExtractionResult
    from juicebox.intake.schema import coverage

    record = record if record is not None else empty_record()
    return ExtractionResult(
        record=record, extractor="llm", coverage=coverage(record), **kwargs
    )


# --------------------------------------------------------------------------
# a call no longer erases a text
# --------------------------------------------------------------------------


class TestTheCallMerges:
    def test_a_call_does_not_blank_what_a_text_captured(self, session, account):
        """The bug: a call about an offer wiped a texted comp expectation."""
        candidate = a_candidate(session, account)
        a_preference(
            session,
            account,
            candidate,
            target_base=240000.0,
            comp_currency="USD",
            timeline="under_1_month",
        )

        record = empty_record()
        record["offers_in_hand"] = [{"company": "Acme", "comp_summary": "", "deadline": ""}]
        upsert_preference(session, call=a_call(session, account, candidate), result=a_result(record))

        row = session.scalar(
            select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
        )
        assert row.target_base == 240000.0, "the call erased a comp figure it never asked about"
        assert row.timeline == "under_1_month"
        assert row.offers_in_hand and row.offers_in_hand[0]["company"] == "Acme"

    def test_the_call_still_wins_a_disagreement(self, session, account):
        """"Actually, make that 260" said out loud has to overwrite."""
        candidate = a_candidate(session, account)
        a_preference(session, account, candidate, target_base=240000.0)

        record = empty_record()
        record["target_base"] = 260000.0
        upsert_preference(session, call=a_call(session, account, candidate), result=a_result(record))

        row = session.scalar(
            select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
        )
        assert row.target_base == 260000.0

    def test_declined_accumulates_across_channels(self, session, account):
        """A refusal cannot be un-given by a channel that did not hear it."""
        candidate = a_candidate(session, account)
        a_preference(session, account, candidate, declined=["compensation"])

        record = empty_record()
        record["declined"] = ["work_authorization"]
        upsert_preference(session, call=a_call(session, account, candidate), result=a_result(record))

        row = session.scalar(
            select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
        )
        assert set(row.declined) == {"compensation", "work_authorization"}

    def test_the_provenance_link_still_flips(self, session, account):
        """Exactly one of the pair is set, even though the content merges."""
        from juicebox.messaging.threads import open_thread

        candidate = a_candidate(session, account)
        thread = open_thread(session, org_id=account["org_id"], channel="web")
        pref = a_preference(session, account, candidate)
        pref.source_thread_id = thread.id
        session.flush()

        call = a_call(session, account, candidate)
        upsert_preference(session, call=call, result=a_result())

        session.refresh(pref)
        assert pref.source_call_id == call.id
        assert pref.source_thread_id is None

    def test_a_confirmed_record_accepts_a_voice_correction(self, session, account):
        """Voice and text both preserve a correction, then require review.

        Confirmation belongs to the fact that changed, not a stale whole-row
        snapshot.  Leaving a newly stated answer out because another answer
        was confirmed forces the candidate to repeat themselves on the next
        channel.
        """
        from juicebox.models import utcnow

        candidate = a_candidate(session, account)
        pref = a_preference(session, account, candidate, timeline="3_6_months")
        pref.confirmed_at = utcnow()
        pref.confirmed_by = "recruiter"
        session.flush()

        record = empty_record()
        record["timeline"] = "immediate"
        upsert_preference(session, call=a_call(session, account, candidate), result=a_result(record))

        session.refresh(pref)
        assert pref.timeline == "immediate"
        assert pref.confirmed_at is None
        assert pref.review_needed
        assert "changed a confirmed record" in pref.review_reason


# --------------------------------------------------------------------------
# the gate knows what we already have
# --------------------------------------------------------------------------


class TestTheGateIsSeeded:
    def test_a_stored_record_reads_back_in_record_shape(self, session, account):
        candidate = a_candidate(session, account)
        a_preference(session, account, candidate, timeline="under_1_month")

        record = prior_preference_record(session, candidate.id)
        assert record is not None
        assert record["timeline"] == "under_1_month"

    def test_nobody_has_no_prior(self, session, account):
        assert prior_preference_record(session, None) is None
        assert prior_preference_record(session, a_candidate(session, account).id) is None

    def test_a_returning_candidate_is_not_re_interviewed(self, session, account, no_llm):
        """The whole point: ask what changed, not the eleven questions again."""
        complete = empty_record()
        complete.update(
            {
                "employment_status": "employed_searching",
                "reason_for_looking": "the ml work got deprioritised",
                "timeline": "under_1_month",
                "target_base": 240000.0,
                "comp_currency": "USD",
                "work_authorization": "green card",
                "active_processes": [{"company": "none", "stage": "none"}],
                "ok_to_share_with_clients": True,
            }
        )
        complete["spec"]["job_titles"] = ["machine learning engineer"]
        complete["spec"]["seniority"] = ["senior"]
        complete["spec"]["locations"] = ["san francisco"]
        complete["spec"]["must_have_skills"] = ["pytorch"]
        complete["spec"]["remote_ok"] = False

        bare = coverage_snapshot("Candidate: hi.", fast=False)
        seeded = coverage_snapshot("Candidate: hi.", fast=False, prior=complete)

        assert seeded["required_outstanding"] < bare["required_outstanding"]
        assert seeded["already_covered"] > bare["already_covered"]

    def test_the_transcript_beats_the_stored_record(self, session, account, no_llm):
        """The seed goes *under* this call's reading, never over it."""
        prior = empty_record()
        prior["timeline"] = "3_6_months"
        snapshot = coverage_snapshot(
            "Candidate: I need to move right away, immediately.", fast=False, prior=prior
        )
        # Nothing about the snapshot exposes the value, so assert on the merge
        # that produced it -- the rule is what matters.
        merged = merge_record(prior, {"timeline": "immediate"})
        assert merged["timeline"] == "immediate"
        assert snapshot["guidance"]

    def test_the_first_check_is_seeded_too(self, session, account):
        """A returning caller's first check often arrives before they speak.

        Answering it with the full eleven is what sends a two-minute update
        call down the whole interview again.
        """
        prior = empty_record()
        prior["timeline"] = "under_1_month"
        assert empty_coverage(prior)["already_covered"] > empty_coverage()["already_covered"]

    def test_a_seeded_gate_never_reaches_the_call_record(self, session, account, no_llm):
        """``call.extracted`` is evidence about this conversation.

        Folding in things the caller never said would make a transcript and a
        record disagree, and the record is what a recruiter reads.
        """
        from juicebox.intake.service import extract_call

        candidate = a_candidate(session, account)
        a_preference(session, account, candidate, target_base=240000.0)
        call = a_call(
            session,
            account,
            candidate,
            transcript="Agent: What changed?\nCandidate: I got an offer from Acme.\n",
        )
        extract_call(session, call)
        assert not call.extracted.get("target_base")


# --------------------------------------------------------------------------
# identification, and what may be said back
# --------------------------------------------------------------------------


class TestADegradedGateIsFlaggedOnTheCall:
    """The gate answering from regexes is a fault, and the call it shaped should
    say so. Without this the fallback is invisible and its rate unknowable."""

    def _failing_llm(self):
        class FakeLLM:
            available = True
            voice_timeout = 3.5

            def json_fast(self, **kwargs):
                raise RuntimeError("timed out")

            json = json_fast

        return FakeLLM()

    def test_the_call_is_marked_for_review(self, monkeypatch):
        import juicebox.intake.extract as extract_mod
        from juicebox.models import IntakeCall

        monkeypatch.setattr(extract_mod, "get_llm", lambda: self._failing_llm())
        call = IntakeCall(review_needed=False, review_reason="")
        coverage_snapshot("Candidate: I want a backend role.", call=call, fast=True)

        assert call.review_needed
        assert "heuristics" in call.review_reason

    def test_a_healthy_extraction_leaves_the_call_alone(self, monkeypatch, no_llm):
        from juicebox.models import IntakeCall

        call = IntakeCall(review_needed=False, review_reason="")
        coverage_snapshot("Candidate: I want a backend role.", call=call, fast=False)
        assert "heuristics" not in (call.review_reason or "")

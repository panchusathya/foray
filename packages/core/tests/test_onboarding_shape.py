"""What onboarding asks, and when.

Two changes drive this file. Onboarding stops at the profile confirm -- no
closing survey -- and the questions that survey used to ask are asked at the
moment an application needs them. And a resume is a first-class way in, not a
page nothing linked to.

The through-line is the same in both: a question is worth asking when the
answer decides something the candidate can see.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.applications import followups
from juicebox.enrich.linkedin_scrape import LinkedInProfile, set_scraper
from juicebox.jobs import KIND_INTAKE_TURN, KIND_LINKEDIN_SCRAPE
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import run_turn
from juicebox.messaging.linkedin_flow import (
    BATCH_CONFIRM,
    FLOW_LINKEDIN_SHORT,
)
from juicebox.messaging.manager import MODE_MANAGING
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import (
    Application,
    Candidate,
    CandidateDocument,
    Job,
    JobPosting,
)
from juicebox.worker import process_job

RESUME_V1 = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@lovelace.dev",
        "location": "San Francisco, CA",
    },
    "summary": "Strategic finance analyst with a banking background.",
    "experience": [
        {
            "company": "Westbridge Capital",
            "title": "Investor",
            "start": "2024-01",
            "end": "Present",
            "bullets": ["Diligence and financial modelling on growth-stage software."],
        }
    ],
    "education": [{"school": "Berkeley", "degree": "BA Economics"}],
    "skills": ["financial modeling", "valuation"],
}


@pytest.fixture
def channel():
    fake = FakeChannel(consent=True)
    set_channel(LINQ, fake)
    return fake


@pytest.fixture
def thread(session, account, channel):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=LINQ,
        provider="linq",
        provider_thread_id="chat-onboard",
        entry_point="linq_inbound",
        from_address="+14155550123",
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
    )


def _say(session, thread, body, native_id):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _drain(session, kind=None, limit=40):
    for _ in range(limit):
        stmt = select(Job).where(Job.status == "queued")
        if kind:
            stmt = stmt.where(Job.kind == kind)
        job = session.scalar(stmt)
        if job is None:
            return
        process_job(session, job)
        session.flush()


class TestOnboardingStopsAtTheConfirm:
    def test_a_profile_with_a_year_count_is_never_asked_for_one(
        self, session, account, thread, channel
    ):
        set_scraper(
            lambda url: LinkedInProfile(
                url=url,
                full_name="Sathya P",
                current_title="Investor",
                current_employer="Westbridge Capital",
                location="San Francisco Bay Area",
                experience_years=1.0,
                is_ic=True,
            )
        )
        _say(session, thread, "hey", "o0")
        _say(session, thread, "sathya@example.com", "o1")
        _say(session, thread, "linkedin.com/in/sathya", "o2")
        _drain(session, KIND_LINKEDIN_SCRAPE)
        run_turn(session, thread=thread)

        confirm = channel.bodies[-1]
        # "1 year", not "1 years" -- this test pinned the plural bug that
        # shipped in the live thread ("about 1 years in").
        assert "about 1 year in" in confirm, "the confirm reads the tenure back"
        assert "1 years" not in confirm
        assert thread.record["spec"]["seniority"] == ["junior"]

        _say(session, thread, "yes", "o3")
        assert "years of experience" not in channel.bodies[-1], (
            f"asked for a number it just quoted: {channel.bodies[-1]!r}"
        )
        assert thread.mode == MODE_MANAGING

    def test_a_correction_after_handover_still_reaches_the_preference(
        self, session, account, thread, channel
    ):
        """The gate stays open by design now, so `_capture` must not wait for it."""
        from juicebox.messaging import identity
        from juicebox.models import CandidatePreference

        thread.contact_email = "corrections@example.com"
        thread.mode = MODE_MANAGING
        thread.record = {
            "spec": {"job_titles": ["investor"], "locations": ["San Francisco"]},
            "declined": [],
        }
        session.flush()
        identity.link_candidate(session, thread=thread)

        _say(session, thread, "i'm actually after strategic finance roles", "c1")

        pref = session.scalar(select(CandidatePreference))
        assert pref is not None, "managing must keep the preference row current"


class TestAResumeIsAWayIn:

    def test_a_parsed_resume_settles_the_profile_step(self, session, account, thread, channel):
        """Upload instead of a URL, and the conversation moves on by itself.

        Before this the upload page took the file, said "got it", and left the
        thread still waiting for a LinkedIn URL it was never going to get.
        """
        _say(session, thread, "hey", "u0")
        _say(session, thread, "ada@lovelace.dev", "u1")

        from juicebox.messaging import identity

        candidate_id = identity.link_candidate(session, thread=thread)
        doc = CandidateDocument(
            org_id=account["org_id"],
            candidate_id=candidate_id,
            kind="resume_uploaded",
            filename="ada.pdf",
            content_type="application/pdf",
            status="uploaded",
            source="upload",
            thread_id=thread.id,
            text_extracted="Ada Lovelace — Investor at Westbridge Capital",
            structured=RESUME_V1,
        )
        session.add(doc)
        session.flush()

        from juicebox.worker import _resume_settles_thread

        candidate = session.get(Candidate, candidate_id)
        from juicebox.enrich.promote import payload_from_resume_v1, promote

        promote(
            session,
            org_id=account["org_id"],
            parsed=payload_from_resume_v1(RESUME_V1, fallback_name="Ada Lovelace"),
            candidate=candidate,
        )
        result = _resume_settles_thread(session, doc=doc, candidate=candidate)
        assert result["advanced"] is True

        assert thread.record["spec"]["job_titles"] == ["Investor"]
        assert session.scalar(
            select(Job).where(Job.kind == KIND_INTAKE_TURN)
        ), "the thread has to be woken so it can confirm"

        run_turn(session, thread=thread)
        assert thread.last_field_asked == BATCH_CONFIRM
        assert "westbridge capital" in channel.bodies[-1].lower()


class TestApplicationQuestionsAskedAtApplicationTime:
    def _application(self, session, account, thread, answers):
        from juicebox.messaging import identity

        thread.contact_email = "ada@lovelace.dev"
        thread.mode = MODE_MANAGING
        session.flush()
        candidate_id = identity.link_candidate(session, thread=thread)
        posting = JobPosting(
            org_id=account["org_id"],
            source="exa",
            source_id="app-posting",
            title="Strategic Finance Analyst",
            company="Google",
            location="San Francisco, CA",
            status="open",
        )
        session.add(posting)
        session.flush()
        application = Application(
            org_id=account["org_id"],
            candidate_id=candidate_id,
            job_posting_id=posting.id,
            status="prepared",
            form_answers=answers,
        )
        session.add(application)
        session.flush()
        return application

    def _blank_set(self):
        return [
            {"key": "work_authorization", "question": "auth?", "answer": "", "edited": False},
            {"key": "salary_expectation", "question": "comp?", "answer": "", "edited": False},
            {"key": "notice_period", "question": "start?", "answer": "", "edited": False},
            {"key": "why_company", "question": "why?", "answer": "", "edited": False},
        ]

    def test_an_answer_we_already_have_is_not_asked_for(
        self, session, account, thread, channel
    ):
        answers = self._blank_set()
        answers[0]["answer"] = "US citizen, no sponsorship needed"
        application = self._application(session, account, thread, answers)
        result = followups.ask_for_missing(session, application)

        assert result.get("skipped") == "package answered everything"
        assert "work_authorization" not in result.get("asked", [])
        assert channel.bodies == []

    def test_it_asks_once(self, session, account, thread, channel):
        application = self._application(session, account, thread, self._blank_set())
        followups.ask_for_missing(session, application)
        before = len(channel.bodies)
        again = followups.ask_for_missing(session, application)

        assert again["skipped"] == "already asked"
        assert len(channel.bodies) == before

    def test_an_answer_that_arrives_later_fills_the_package(
        self, session, account, thread, channel
    ):
        from juicebox.models import CandidatePreference

        application = self._application(session, account, thread, self._blank_set())
        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=application.candidate_id,
                spec={},
                target_base=200000,
                comp_currency="USD",
                timeline="immediate",
            )
        )
        session.flush()

        filled = followups.refill_from_preference(session, application)
        assert "salary_expectation" in filled
        answers = {i["key"]: i["answer"] for i in application.form_answers}
        assert "200,000" in answers["salary_expectation"]
        assert answers["notice_period"] == "immediate"

    def test_a_human_edit_is_never_overwritten(self, session, account, thread):
        from juicebox.models import CandidatePreference

        answers = self._blank_set()
        answers[1]["edited"] = True
        application = self._application(session, account, thread, answers)
        session.add(
            CandidatePreference(
                org_id=account["org_id"],
                candidate_id=application.candidate_id,
                spec={},
                target_base=200000,
            )
        )
        session.flush()

        filled = followups.refill_from_preference(session, application)
        assert "salary_expectation" not in filled


class TestADegradedEmbedderIsNotSilent:
    """A wrong Voyage key looks exactly like a healthy one from outside.

    Falling back to hashing keeps a single search alive, which is right. But
    hash cosines carry no meaning, the vector term is 0.30 of the blend, and
    the only downstream symptom is every candidate being told nothing clears
    the bar -- so the fallback has to be visible somewhere.
    """

    def test_health_reports_the_fallback(self, client):
        from juicebox.llm import embeddings as embeddings_module

        broken = embeddings_module.VoyageEmbedder("pa-wrong", "voyage-3-lite", 512)
        previous = embeddings_module._embedder
        embeddings_module._embedder = broken
        try:
            clean = client.get("/health").json()
            assert clean["embeddings_degraded"] is False

            broken.degraded_reason = "403 Forbidden"
            broken.degraded_calls = 1
            body = client.get("/health").json()
            assert body["embeddings_degraded"] is True
            assert "403" in body["embeddings_reason"]
        finally:
            embeddings_module._embedder = previous


class TestChatAsksOnlyWorkAuthorization:
    """Salary, start date, and why-this-company are not surveyed over text."""

    class _Posting:
        company = "Voice Capital"
        title = "Founding General Partner"

    def _application(self, answers):
        class _App:
            form_answers = answers

        return _App()

    def test_missing_work_auth_is_the_only_question(self):
        from juicebox.applications.followups import followup_message

        answers = [
            {"key": "work_authorization", "answer": ""},
            {"key": "salary_expectation", "answer": ""},
            {"key": "why_company", "answer": ""},
        ]
        body = followup_message(self._application(answers), self._Posting())
        assert "authoris" in body.lower() or "sponsor" in body.lower()
        assert "Voice Capital" in body
        assert "comp" not in body.lower()
        assert "why Voice Capital" not in body
        assert "write it in the app" not in body

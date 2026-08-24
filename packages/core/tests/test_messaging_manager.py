"""The recruiting-manager modes, end to end on fakes.

Two invariants above all: **the intake mode is untouched** (its own 1300-line
suite is the pin; here we only check a manager thread cannot leak into it),
and **a managing thread never completes** — run_turn is silent forever on a
complete thread, and a recruiting manager's line going dead is a product
failure, not an end state.

Everything runs with no API key: the keyword intent pass, canned questions,
template packages. Flatter, not broken.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.intake.schema import empty_record
from juicebox.jobs import KIND_APP_PACKAGE, KIND_PROFILE_INGEST
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import ACTION_HELD, run_turn
from juicebox.messaging.manager import (
    MODE_MANAGING,
    MODE_ONBOARDING,
    MODE_PROFILE,
)
from juicebox.messaging.threads import (
    STATUS_COMPLETE,
    open_thread,
    record_inbound,
)
from juicebox.models import (
    Application,
    Job,
    JobMatch,
    JobPosting,
    JobProposal,
)

STRONG = {
    "name": "title", "label": "Title", "kind": "title", "weight": 1.0,
    "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": [],
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
        provider_thread_id="chat-mgr",
        entry_point="linq_inbound",
        from_address="+14155550100",
        consent_messaging=True,
        mode=MODE_ONBOARDING,
    )


def _say(session, thread, body, *, native_id=""):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


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
    return record


def _posting(session, org_id, *, company="Anthropic", title="ML Engineer", suffix="1"):
    posting = JobPosting(
        org_id=org_id,
        source="manual",
        source_id=f"mgr-{company.lower()}-{suffix}",
        title=title,
        company=company,
        company_normalized=company.lower(),
        jd_text="Build ML systems at scale.",
        status="open",
    )
    session.add(posting)
    session.flush()
    return posting


def _managed(session, account, channel, thread):
    """Fast-forward a thread to managing with a linked candidate."""
    thread.contact_name = "Ada Okonkwo"
    thread.contact_email = "ada-mgr@example.com"
    thread.record = _core_complete_record()
    thread.mode = MODE_MANAGING
    from juicebox.messaging import identity

    identity.link_candidate(session, thread=thread)
    session.flush()
    return thread


class TestOnboarding:
    def test_greeting_identifies_org_ai_and_exits(self, session, account, channel, thread):
        result = _say(session, thread, "hey")
        assert result.asked_field == "contact.email"
        greeting = channel.bodies[0]
        assert "Acme Talent" in greeting  # the org, because the number is shared
        assert "AI" in greeting
        assert "STOP" in greeting

    def test_the_walk_to_profile(self, session, account, channel, thread):
        _say(session, thread, "hey")
        _say(session, thread, "ada-mgr@example.com")
        assert thread.contact_email == "ada-mgr@example.com"
        result = _say(session, thread, "Ada Okonkwo")
        assert result.asked_field == "contact.linkedin"
        assert "/v1/intake/uploads/" in channel.bodies[-1]  # the signed upload link

        result = _say(session, thread, "https://linkedin.com/in/ada-okonkwo")
        assert thread.linkedin_url == "https://www.linkedin.com/in/ada-okonkwo"
        assert thread.mode == MODE_PROFILE
        job = session.scalar(select(Job).where(Job.kind == KIND_PROFILE_INGEST))
        assert job is not None and job.payload["thread_id"] == thread.id
        # The turn keeps moving: the ack rides the first profile question.
        assert result.asked_field

    def test_profile_completes_into_managing_not_complete(
        self, session, account, channel, thread
    ):
        thread.contact_name = "Ada Okonkwo"
        thread.contact_email = "ada-mgr@example.com"
        thread.linkedin_url = "https://www.linkedin.com/in/ada-okonkwo"
        thread.mode = MODE_PROFILE
        thread.record = _core_complete_record()
        session.flush()

        result = _say(session, thread, "that's everything")
        assert thread.mode == MODE_MANAGING
        # THE invariant: the thread is permanent. Only mode=intake completes.
        assert thread.status != STATUS_COMPLETE
        assert result.action == "completed_profile"
        assert thread.candidate_id  # linked, so ingest and matching can land

    def test_held_when_last_message_is_ours(self, session, account, channel, thread):
        _say(session, thread, "hey")
        # No new inbound: the shared rail holds manager modes too.
        result = run_turn(session, thread=thread)
        assert result.action == ACTION_HELD


class TestManaging:
    def test_present_apply_and_status(self, session, account, channel, thread):
        thread = _managed(session, account, channel, thread)
        org_id = account["org_id"]
        for index, company in enumerate(("Anthropic", "Figma")):
            posting = _posting(session, org_id, company=company, suffix=str(index))
            session.add(
                JobMatch(
                    org_id=org_id,
                    job_posting_id=posting.id,
                    candidate_id=thread.candidate_id,
                    score=0.9 - index * 0.05,
                    criteria=[STRONG],
                    status="new",
                )
            )
        session.flush()

        result = _say(session, thread, "find me some roles", native_id="m-present")
        assert result.action == "presented"
        body = channel.bodies[-1]
        assert "1. ML Engineer — Anthropic" in body
        assert "Current title matches" in body
        assert "0.9" not in body  # never a score
        assert "/m/" in body  # the portal deep link
        frame = thread.last_presented or []
        assert [entry["company"] for entry in frame] == ["Anthropic", "Figma"]
        proposals = session.scalars(select(JobProposal)).all()
        assert len(proposals) == 2
        assert all(p.status == "proposed" and p.message_id for p in proposals)

        # "apply to 2" is the per-application yes, bound to that message.
        result = _say(session, thread, "apply to 2", native_id="m-apply")
        assert result.action == "applied"
        application = session.scalar(select(Application))
        assert application is not None
        posting = session.get(JobPosting, application.job_posting_id)
        assert posting.company == "Figma"
        assert application.consenting_message_id is not None
        assert application.consent_verbatim == "apply to 2"
        accepted = session.get(JobProposal, application.proposal_id)
        assert accepted.status == "accepted"
        package_job = session.scalar(select(Job).where(Job.kind == KIND_APP_PACKAGE))
        assert package_job is not None
        assert "Figma" in channel.bodies[-1]

        result = _say(session, thread, "what's my status", native_id="m-status")
        assert "Figma" in channel.bodies[-1]

    def test_ambiguous_apply_gets_a_clarifier_not_a_guess(
        self, session, account, channel, thread
    ):
        thread = _managed(session, account, channel, thread)
        thread.last_presented = [
            {"proposal_id": "", "posting_id": "x", "company": "Acme", "title": "Engineer"},
            {"proposal_id": "", "posting_id": "y", "company": "Acme Labs", "title": "Engineer"},
        ]
        session.flush()
        _say(session, thread, "apply to the acme one", native_id="m-ambig")
        assert "number" in channel.bodies[-1].lower()
        assert session.scalar(select(Application)) is None  # no guessed pursuit

    def test_presented_roles_never_resurface(self, session, account, channel, thread):
        thread = _managed(session, account, channel, thread)
        org_id = account["org_id"]
        posting = _posting(session, org_id, suffix="once")
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
        _say(session, thread, "find me roles", native_id="m-a")
        assert "Anthropic" in channel.bodies[-1]
        _say(session, thread, "more roles please", native_id="m-b")
        assert "Nothing new clears the bar" in channel.bodies[-1]

    def test_other_intent_gets_the_canned_deflection(self, session, account, channel, thread):
        thread = _managed(session, account, channel, thread)
        _say(session, thread, "can you fix my printer", native_id="m-other")
        assert "recruiter will follow up" in channel.bodies[-1]


class TestIntakeUntouched:
    def test_web_intake_never_reaches_the_manager(self, session, account):
        from juicebox.messaging.channels import WEB

        set_channel(WEB, FakeChannel())
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        assert thread.mode == "intake"
        result = run_turn(session, thread=thread)
        # The classic greet + first intake question, exactly as before.
        assert result.action == "greeted"

"""The recruiting manager: what happens after onboarding finishes.

Onboarding itself is the LinkedIn-first flow (``test_linkedin_intake.py``).
This suite covers the seam the merge created and the steady state after it:

* onboarding **hands over** to ``managing`` instead of completing, because a
  complete thread is a silent one and a recruiting manager going silent
  mid-search is the product failing,
* the shared rails — hold, quiet hours — apply to *both* flows, and the
  LinkedIn path returning early must not dodge them,
* and managing itself: cards, "apply to 2" as the per-application yes,
  status, networking.

Everything runs with no API key: keyword intents, canned copy, template
packages. Flatter, not broken.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from juicebox.enrich.linkedin_scrape import LinkedInProfile, set_scraper
from juicebox.intake.schema import empty_record
from juicebox.jobs import KIND_APP_PACKAGE, KIND_LINKEDIN_SCRAPE
from juicebox.messaging import cadence
from juicebox.messaging.channels import LINQ, FakeChannel, set_channel
from juicebox.messaging.conversation import ACTION_DEFERRED, ACTION_HELD, run_turn
from juicebox.messaging.linkedin_flow import FLOW_LINKEDIN_SHORT
from juicebox.messaging.manager import MODE_INTAKE, MODE_MANAGING
from juicebox.messaging.threads import STATUS_COMPLETE, open_thread, record_inbound
from juicebox.models import Application, Job, JobMatch, JobPosting, JobProposal
from juicebox.worker import process_job

STRONG = {
    "name": "title", "label": "Title", "kind": "title", "weight": 1.0,
    "status": "strong", "score": 0.95, "evidence": "Current title matches", "sources": [],
}

# Winter, so the unknown-timezone window (11:00-21:00 America/New_York) is
# 16:00Z-02:00Z.
OUT_OF_WINDOW = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


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
        provider_thread_id="chat-mgr",
        entry_point="linq_inbound",
        from_address="+14155550100",
        consent_messaging=True,
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
    record["timeline"] = "under_1_month"
    record["work_authorization"] = "green card"
    record["ok_to_share_with_clients"] = True
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


def _managed(session, thread):
    """Fast-forward past onboarding: linked candidate, managing mode."""
    from juicebox.messaging import identity

    thread.contact_name = "Ada Okonkwo"
    thread.contact_email = "ada-mgr@example.com"
    thread.record = _core_complete_record()
    thread.mode = MODE_MANAGING
    identity.link_candidate(session, thread=thread)
    session.flush()
    return thread


class TestOnboardingDefaults:
    def test_a_linq_thread_starts_on_the_linkedin_flow(self, session, thread):
        """The webhook sets no mode: `flow` drives onboarding, and a second
        opinion about which phase we are in is how the two rot apart."""
        assert thread.flow == FLOW_LINKEDIN_SHORT
        assert thread.mode == MODE_INTAKE

    def test_the_first_message_says_how_to_stop(self, session, thread, channel):
        """CANDIDATE_AGENT_PLAN §2: one shared number means the candidate
        cannot tell who is texting, so the opening line carries who we are,
        that this is an AI, and the way out."""
        _say(session, thread, "hey")
        intro = channel.bodies[0]
        # The channel speaks lowercase; the carrier keywords are held out of
        # that transform on purpose, so they are asserted as written.
        assert "acme talent" in intro.lower()
        assert "ai" in intro.lower()
        assert "STOP" in intro
        assert "HELP" in intro


class TestSharedRails:
    """The merge's real risk: the LinkedIn path returns early in run_turn."""

    def test_quiet_hours_defer_the_linkedin_flow_too(
        self, session, thread, channel, monkeypatch
    ):
        record_inbound(session, thread=thread, body="hi", provider_native_id="q1")
        thread.last_inbound_at = OUT_OF_WINDOW - timedelta(hours=3)
        monkeypatch.setattr(cadence, "utcnow", lambda: OUT_OF_WINDOW)

        result = run_turn(session, thread=thread)
        assert result.action == ACTION_DEFERRED
        assert channel.bodies == []  # nothing sent at 3am

    def test_hold_applies_before_either_flow(self, session, thread, channel):
        _say(session, thread, "hey")
        # No new inbound: it is their turn, whichever flow is running.
        assert run_turn(session, thread=thread).action == ACTION_HELD


class TestHandover:
    def test_onboarding_hands_over_instead_of_completing(self, session, thread, channel):
        """The invariant this merge exists to preserve."""
        set_scraper(
            lambda url: LinkedInProfile(
                url=url,
                full_name="Ada Okonkwo",
                current_title="Machine Learning Engineer",
                current_employer="Modal",
                location="San Francisco",
                experience_years=8.0,
            )
        )
        _say(session, thread, "hey", native_id="h1")
        _say(session, thread, "ada-mgr@example.com", native_id="h2")
        _say(session, thread, "https://linkedin.com/in/ada-okonkwo", native_id="h3")

        job = session.scalar(select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE))
        assert job is not None
        process_job(session, job)
        run_turn(session, thread=thread)  # confirm message

        # Walk the rest of the flow to its end.
        for index, reply in enumerate(("yep that's right", "240k, a month, green card, yes")):
            _say(session, thread, reply, native_id=f"h-tail-{index}")

        session.refresh(thread)
        assert thread.status != STATUS_COMPLETE, "a manager thread must never go silent"
        assert thread.mode == MODE_MANAGING
        assert thread.candidate_id


class TestManaging:
    def test_present_apply_and_status(self, session, account, channel, thread):
        thread = _managed(session, thread)
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
        assert "/m/" in body
        # Never a score. Card lines only: the link is a JWT whose random bytes
        # contain "0.9" about 1.5% of the time.
        assert "0.9" not in _card_lines(body)

        frame = thread.last_presented or []
        assert [entry["company"] for entry in frame] == ["Anthropic", "Figma"]
        assert len(session.scalars(select(JobProposal)).all()) == 2

        result = _say(session, thread, "apply to 2", native_id="m-apply")
        assert result.action == "applied"
        application = session.scalar(select(Application))
        assert session.get(JobPosting, application.job_posting_id).company == "Figma"
        # The per-application yes, bound to the message it arrived in.
        assert application.consenting_message_id is not None
        assert application.consent_verbatim == "apply to 2"
        assert session.get(JobProposal, application.proposal_id).status == "accepted"
        assert session.scalar(select(Job).where(Job.kind == KIND_APP_PACKAGE)) is not None

        _say(session, thread, "what's my status", native_id="m-status")
        assert "Figma" in channel.bodies[-1]

    def test_ambiguous_apply_gets_a_clarifier_not_a_guess(self, session, channel, thread):
        thread = _managed(session, thread)
        thread.last_presented = [
            {"proposal_id": "", "posting_id": "x", "company": "Acme", "title": "Engineer"},
            {"proposal_id": "", "posting_id": "y", "company": "Acme Labs", "title": "Engineer"},
        ]
        session.flush()
        _say(session, thread, "apply to the acme one", native_id="m-ambig")
        assert "number" in channel.bodies[-1].lower()
        assert session.scalar(select(Application)) is None

    def test_presented_roles_never_resurface(self, session, account, channel, thread):
        thread = _managed(session, thread)
        posting = _posting(session, account["org_id"], suffix="once")
        session.add(
            JobMatch(
                org_id=account["org_id"],
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
        # Not shown twice -- and asking again with an empty book sends us
        # looking rather than answering from it, so the reply is the search
        # line, not the card.
        assert "Anthropic" not in channel.bodies[-1]
        assert "looking now" in channel.bodies[-1].lower()

    def test_other_intent_gets_the_canned_deflection(self, session, channel, thread):
        thread = _managed(session, thread)
        _say(session, thread, "can you fix my printer", native_id="m-other")
        assert "recruiter will follow up" in channel.bodies[-1]


def _card_lines(body: str) -> str:
    return "\n".join(line for line in body.splitlines() if "/m/" not in line)


class TestIntakeUntouched:
    def test_web_intake_still_runs_the_classic_flow(self, session, account):
        from juicebox.messaging.channels import WEB

        set_channel(WEB, FakeChannel())
        thread = open_thread(
            session, org_id=account["org_id"], channel=WEB, consent_messaging=True
        )
        assert thread.mode == MODE_INTAKE
        assert thread.flow == "full"
        assert run_turn(session, thread=thread).action == "greeted"


class TestTheFirstLiveThread:
    """Regression pins from the first real conversation (2026-08-24, 19:09).

    That thread asked for a name, was given one, and dropped it -- so nothing
    linked a candidate, nothing was ever matched, and "find me roles" answered
    "tell me your name and email first" to somebody who had supplied both. It
    then sent the identical canned line on two consecutive turns.
    """

    def test_the_gap_fill_answer_is_not_thrown_away(self, session, thread, channel):
        """"Sathya, 2 yrs, 150k" carries a name, and the name has to survive.

        This is the root cause of the whole cascade: no name, no candidate, no
        matches, no roles.
        """
        from juicebox.messaging import identity
        from juicebox.messaging.linkedin_flow import BATCH_GAP_FILL

        thread.contact_email = "ada@example.com"
        thread.last_field_asked = BATCH_GAP_FILL
        session.flush()

        identity.absorb(
            thread,
            body="Sathya, 2 yrs, 150k",
            asked_field=BATCH_GAP_FILL,
            batched=True,
        )
        assert thread.contact_name == "Sathya"

    def test_a_partial_batch_answer_invents_no_name(self, session, thread):
        """Skipping the name question must leave it unset, not guess.

        "150k, asap" answers the other two questions. Reading the first
        name-shaped token out of it would put a candidate called Asap in front
        of an employer.
        """
        from juicebox.messaging import identity
        from juicebox.messaging.linkedin_flow import BATCH_GAP_FILL

        identity.absorb(
            thread, body="150k, asap", asked_field=BATCH_GAP_FILL, batched=True
        )
        assert not thread.contact_name

    def test_an_email_alone_is_enough_to_link(self, session, account, channel, thread):
        """No name question. An address identifies somebody on its own.

        The manager used to demand "name and email" from people who had given
        an email, then drop the answer. Now the email links the candidate and
        the name arrives with the LinkedIn profile.
        """
        thread.record = _core_complete_record()
        thread.contact_email = "ada-live@example.com"
        thread.mode = MODE_MANAGING
        session.flush()

        _say(session, thread, "find me roles", native_id="live-1")
        session.refresh(thread)
        assert thread.candidate_id, "an email is enough to link"
        assert "name" not in channel.bodies[-1].lower(), "must not ask for a name"

    def test_it_still_asks_when_it_has_nothing(self, session, account, channel, thread):
        """The one case a name is worth asking for: no email, no LinkedIn."""
        thread.record = _core_complete_record()
        thread.contact_email = ""
        thread.contact_linkedin = ""
        thread.mode = MODE_MANAGING
        session.flush()

        _say(session, thread, "find me roles", native_id="live-3")
        assert "email" in channel.bodies[-1].lower()

    def test_never_sends_the_same_canned_line_twice(self, session, account, channel, thread):
        """Two identical replies in a row is what made the agent look broken."""
        thread = _managed(session, thread)

        _say(session, thread, "i also care about equity", native_id="rep-1")
        first = channel.bodies[-1]
        _say(session, thread, "and remote", native_id="rep-2")
        second = channel.bodies[-1]
        assert first != second, f"repeated verbatim: {first!r}"

    def test_no_is_a_refusal_not_a_profile_answer(self, session):
        """"No" was reaching the LLM and coming back as profile_answer."""
        from juicebox.messaging import intents

        for text in ("No", "no", "nope", "nah", "not now"):
            assert intents.classify(text, presented=[]).kind == intents.INTENT_DECLINE, text
        for text in ("yes", "Yep", "sure", "ok"):
            assert intents.classify(text, presented=[]).kind == intents.INTENT_AFFIRM, text

    def test_no_inside_a_sentence_is_not_a_refusal(self):
        """Whole-message only: "no rush, show me more" is a request."""
        from juicebox.messaging import intents

        assert intents.classify("no rush, show me more", presented=[]).kind != (
            intents.INTENT_DECLINE
        )


class TestConversationalVoice:
    """The flow used to send templates only, which is why it read as a survey.

    The model now chooses the words; the code still chooses what is said and
    when. Everything here is about that boundary holding.
    """

    def _llm(self, monkeypatch, message):
        import juicebox.llm.client as client

        class FakeLLM:
            available = True

            def json_fast(self, **kwargs):
                from types import SimpleNamespace

                return SimpleNamespace(data={"message": message})

        monkeypatch.setattr(client, "get_llm", lambda: FakeLLM())
        import juicebox.messaging.conversation as conv

        monkeypatch.setattr(conv, "get_llm", lambda: FakeLLM())

    def test_no_model_means_the_canned_copy(self, session, thread):
        from juicebox.messaging.conversation import humanize

        canned = "- what are you targeting on comp (base is fine)?"
        assert humanize(thread, says=canned, last_inbound="hi") == canned

    def test_a_rewrite_is_used(self, session, thread, monkeypatch):
        from juicebox.messaging.conversation import humanize

        self._llm(monkeypatch, "got it sathya — what are you targeting on comp?")
        out = humanize(thread, says="- comp?", last_inbound="i'm sathya")
        assert out.startswith("got it sathya")

    def test_an_invented_question_is_refused(self, session, thread, monkeypatch):
        """The guard the batched length check cannot provide.

        A model asked for one question sometimes sends two, and the extra one
        gets a real answer that lands in the record as though we meant to ask.
        """
        from juicebox.messaging.conversation import humanize

        canned = "- what are you targeting on comp (base is fine)?"
        self._llm(
            monkeypatch,
            "got it — what are you targeting on comp? and are you open to relocating?",
        )
        assert humanize(thread, says=canned, last_inbound="hi") == canned

    def test_an_over_long_rewrite_is_refused(self, session, thread, monkeypatch):
        from juicebox.messaging.conversation import humanize

        canned = "- comp?"
        self._llm(monkeypatch, "x" * 5000)
        assert humanize(thread, says=canned, last_inbound="hi") == canned

    def test_nothing_to_react_to_means_no_call(self, session, thread, monkeypatch):
        """No inbound, no rewrite: there is nothing to acknowledge."""
        from juicebox.messaging.conversation import humanize

        self._llm(monkeypatch, "should not be used")
        canned = "- comp?"
        assert humanize(thread, says=canned, last_inbound="") == canned

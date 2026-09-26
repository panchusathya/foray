"""LinkedIn-first text intake and Linq delivery/read receipts."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_messaging import LINQ_OUR_NUMBER, LINQ_WEBHOOK_SECRET, _say, post_linq

from juicebox.enrich.linkedin_scrape import LinkedInProfile, set_scraper
from juicebox.jobs import KIND_LINKEDIN_SCRAPE
from juicebox.messaging.channels import WEB, set_channel
from juicebox.messaging.conversation import (
    ACTION_ASKED,
    run_turn,
)
from juicebox.messaging.identity import (
    CONTACT_LINKEDIN,
    link_candidate,
)
from juicebox.messaging.linkedin_flow import (
    BATCH_CONFIRM,
    BATCH_GAP_FILL,
    FLOW_LINKEDIN_SHORT,
)
from juicebox.messaging.prompts import lowercase_outbound
from juicebox.messaging.threads import open_thread
from juicebox.models import Candidate, IntakeMessage, IntakeNumber, Job
from juicebox.repository import owner_of_linkedin, upsert_candidates
from juicebox.worker import process_job


def _short_thread(session, account, fake_channel, **kwargs):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=WEB,
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
        **kwargs,
    )


def _profile(**overrides) -> LinkedInProfile:
    data = dict(
        url="https://www.linkedin.com/in/ada-lovelace",
        full_name="Ada Lovelace",
        headline="Software Engineer at Stripe",
        current_title="Software Engineer",
        current_employer="Stripe",
        location="San Francisco",
        experience_years=8.0,
        is_ic=True,
    )
    data.update(overrides)
    return LinkedInProfile(**data)


def _finish_scrape(session, thread, profile=None):
    set_scraper(lambda url: profile or _profile(url=url))
    job = session.scalars(select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE)).one()
    process_job(session, job)
    return run_turn(session, thread=thread)


@pytest.fixture
def linq_signing_env(monkeypatch):
    monkeypatch.setenv("LINQ_WEBHOOK_SECRET", LINQ_WEBHOOK_SECRET)
    monkeypatch.setenv("LINQ_API_KEY", "linq_test_api_key")
    monkeypatch.setenv("LINQ_FROM_NUMBER", LINQ_OUR_NUMBER)
    monkeypatch.setenv("LINQ_ENABLED", "true")
    from juicebox.config import reset_settings_cache
    from juicebox.messaging.channels import reset_channels

    reset_settings_cache()
    reset_channels()
    yield
    reset_settings_cache()
    reset_channels()


@pytest.fixture
def linq_number(session, account):
    row = IntakeNumber(
        org_id=account["org_id"],
        provider="linq",
        phone=LINQ_OUR_NUMBER,
        label="linq intake",
    )
    session.add(row)
    session.commit()
    return row


class TestLinkedinShortFlow:
    def test_intro_discloses_and_asks_for_the_profile_in_one_turn(
        self, session, account, fake_channel
    ):
        """The greeting bubble, then the profile ask riding the same turn.

        The intro no longer ends on "what's the best email" — the email comes
        verified from the OAuth callback, or in the gap-fill for manual
        threads. A stranger's first task is no longer typing an address.
        """
        thread = _short_thread(session, account, fake_channel)
        result = run_turn(session, thread=thread)

        assert result.action == ACTION_ASKED
        assert result.asked_field == CONTACT_LINKEDIN
        assert len(fake_channel.bodies) == 2
        body = fake_channel.bodies[0]
        # Lowercase voice, with two deliberate hold-outs: URLs, and the carrier
        # keywords. The keywords are held out of the transform whether or not
        # any message currently contains one -- see ``lowercase_outbound``.
        prose = body.replace("STOP", "").replace("HELP", "")
        assert prose == prose.lower() or "http" in body
        assert "email" not in body
        # The persona name comes from settings, so this asserts whatever the
        # agent is currently called rather than pinning one spelling.
        from juicebox.messaging.prompts import recruiter_name

        assert recruiter_name().lower() in body
        # The two disclosures that survive: who we are, and that this is an AI.
        # The opt-out recital that used to be the third is gone --
        # INTAKE_COMPLIANCE.md section 6a has the reasoning and the exposure,
        # and ``test_intake_consent_copy.py`` is where this is now asserted
        # across every greeting at once.
        assert "acme talent" in body
        assert "ai" in body
        assert "STOP" not in body and "HELP" not in body


    def test_the_confirm_answer_wraps_up_and_links_the_candidate(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")
        _finish_scrape(session, thread)
        result = _say(session, thread, "yes that's right")

        # Onboarding is over, the relationship is not: the thread hands over
        # to the recruiting manager instead of completing. A complete thread
        # is a silent one -- run_turn never speaks to it again -- which is the
        # right end for a careers-page intake and the wrong one for somebody
        # who now expects job cards.
        assert result.action == "completed_profile"
        assert thread.mode == "managing"
        assert thread.status != "complete"
        assert thread.record["spec"]["seniority"] == ["staff"]
        assert thread.candidate_id
        candidate = session.get(Candidate, thread.candidate_id)
        assert candidate.linkedin_url == "https://www.linkedin.com/in/ada-lovelace"
        assert candidate.crm_tracked is True

    def test_scrape_failure_falls_open_to_questions(
        self, session, account, fake_channel
    ):
        from juicebox.enrich.linkedin_scrape import ScrapeError

        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")

        def boom(_url):
            raise ScrapeError("auth wall")

        set_scraper(boom)
        job = session.scalars(select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE)).one()
        process_job(session, job)
        result = run_turn(session, thread=thread)

        assert result.asked_field == BATCH_GAP_FILL
        assert "couldn't pull" in fake_channel.bodies[-1]


class TestLinkedinIdentity:
    def test_link_candidate_matches_an_existing_linkedin_url(
        self, session, account, fake_channel
    ):
        existing = upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "test",
                    "source_id": "ada",
                    "full_name": "Ada Lovelace",
                    "linkedin_url": "https://www.linkedin.com/in/ada-lovelace",
                }
            ],
        ).ids[0]
        thread = _short_thread(
            session,
            account,
            fake_channel,
            contact_name="Ada Lovelace",
            contact_linkedin="https://www.linkedin.com/in/ada-lovelace",
        )
        linked = link_candidate(session, thread=thread)
        assert linked == existing
        assert owner_of_linkedin(
            session, org_id=account["org_id"], url="linkedin.com/in/ada-lovelace"
        ) == existing


class TestLinkedinCopy:
    def test_urls_survive_lowercasing(self):
        text = lowercase_outbound("See HTTPS://www.LinkedIn.com/in/Ada")
        assert "HTTPS://www.LinkedIn.com/in/Ada" in text
        assert text.startswith("see ")


class TestLinqReceipts:
    @pytest.fixture(autouse=True)
    def _linq_env(self, linq_signing_env):
        yield

    def test_delivered_and_read_stamp_the_outbound_row(
        self, client, session, linq_number, account, fake_channel
    ):
        from juicebox.messaging.channels import LINQ

        set_channel(LINQ, fake_channel)
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-1",
            consent_messaging=True,
            flow=FLOW_LINKEDIN_SHORT,
        )
        run_turn(session, thread=thread)
        outbound = session.scalars(
            select(IntakeMessage)
            .where(
                IntakeMessage.thread_id == thread.id,
                IntakeMessage.direction == "outbound",
            )
            .order_by(IntakeMessage.created_at.desc())
        ).first()
        native_id = outbound.provider_native_id
        session.commit()

        delivered = {
            "event_type": "message.delivered",
            "event_id": "evt-del",
            "data": {
                "id": native_id,
                "delivered_at": "2026-02-05T19:13:50.000Z",
                "read_at": None,
            },
        }
        assert post_linq(client, delivered).status_code == 200
        session.refresh(outbound)
        assert outbound.delivered_at is not None
        assert outbound.status == "delivered"

        read = {
            "event_type": "message.read",
            "event_id": "evt-read",
            "data": {
                "id": native_id,
                "delivered_at": "2026-02-05T19:13:50.000Z",
                "read_at": "2026-02-05T19:13:58.177Z",
            },
        }
        assert post_linq(client, read).status_code == 200
        session.refresh(outbound)
        assert outbound.read_at is not None
        assert outbound.status == "read"

        # Duplicate event is a no-op, timestamps stay.
        again = post_linq(client, read)
        assert again.status_code == 200
        session.refresh(outbound)
        assert outbound.status == "read"


    def test_message_failed_marks_the_row(
        self, client, session, linq_number, account, fake_channel
    ):
        from juicebox.messaging.channels import LINQ

        set_channel(LINQ, fake_channel)
        thread = open_thread(
            session,
            org_id=account["org_id"],
            channel=LINQ,
            provider="linq",
            provider_thread_id="chat-fail",
            consent_messaging=True,
            flow=FLOW_LINKEDIN_SHORT,
        )
        run_turn(session, thread=thread)
        outbound = session.scalars(
            select(IntakeMessage).order_by(IntakeMessage.created_at.desc())
        ).first()
        session.commit()
        payload = {
            "event_type": "message.failed",
            "data": {
                "id": outbound.provider_native_id,
                "error": {"reason": "undeliverable"},
            },
        }
        assert post_linq(client, payload).status_code == 200
        session.refresh(outbound)
        assert outbound.status == "failed"
        assert "undeliverable" in outbound.error


class TestTargetConfirmation:
    """Read back what we will search for, not just who they are.

    `apply_profile_to_record` turns the current title into `spec.job_titles`,
    which is what the job search runs on -- so the standing assumption is that
    everybody wants more of the job they already have. People text a recruiter
    precisely when that is false. This is the last cheap moment to fix it:
    after it we spend search credits, build a package, and approach an
    employer on that basis.
    """

    def test_a_correction_replaces_the_target_rather_than_adding_to_it(self):
        """The revision has to win outright.

        If a corrected title merged alongside the scraped one we would search
        for the job they just told us they did not want, and propose it.
        """
        from juicebox.messaging.records import merge_record

        scraped = merge_record(
            None, {"spec": {"job_titles": ["machine learning engineer"]}}
        )
        corrected = merge_record(scraped, {"spec": {"job_titles": ["product manager"]}})
        assert corrected["spec"]["job_titles"] == ["product manager"]

    def test_saying_nothing_new_keeps_the_scraped_target(self):
        """"yep that's right" must not wipe what the profile gave us."""
        from juicebox.messaging.records import merge_record

        scraped = merge_record(None, {"spec": {"job_titles": ["ml engineer"]}})
        after = merge_record(scraped, {"spec": {"job_titles": []}})
        assert after["spec"]["job_titles"] == ["ml engineer"]


class TestRejectedTarget:
    """What happens when the candidate says the inferred target is wrong.

    From the live thread: "No I want to switch to Strat fin" left
    `job_titles: ["Investor"]` untouched and the agent replied "you're set".
    We asked, ignored the answer, and went looking for the job they had just
    rejected. Asking a question and discarding the reply is worse than never
    asking it.
    """

    def test_a_rejection_is_recognised(self):
        from juicebox.messaging.linkedin_flow import looks_like_correction

        for text in (
            "No I want to switch to Strat fin",
            "no, strategic finance",
            "actually product roles",
            "Nope",
            "not really",
        ):
            assert looks_like_correction(text), text

    def test_agreement_is_not_a_rejection(self):
        from juicebox.messaging.linkedin_flow import looks_like_correction

        for text in ("yep that's right", "sounds right", "yes please", ""):
            assert not looks_like_correction(text), text

    def test_no_mid_sentence_false_positive(self):
        """"the comp is not negotiable" agrees with the read-back."""
        from juicebox.messaging.linkedin_flow import looks_like_correction

        assert not looks_like_correction("the comp is not negotiable")

    def test_the_live_sentence_yields_its_title(self):
        """Stacked lead-ins: "no" + "i want to" + "switch to" then the title.

        Stripping one leaves seven words, which the length guard discards --
        which is precisely how this sentence was lost.
        """
        from juicebox.messaging.linkedin_flow import title_from_reply

        assert title_from_reply("No I want to switch to Strat fin") == "Strat fin"

    def test_the_title_is_kept_as_they_said_it(self):
        """No taxonomy mapping. The Exa query is a text search, and normalising
        "strat fin" into something we recognise is how it got lost."""
        from juicebox.messaging.linkedin_flow import title_from_reply

        assert title_from_reply("no, strategic finance") == "strategic finance"
        assert title_from_reply("actually product manager roles") == "product manager"


    def test_clearing_beats_keeping_a_rejected_title(self):
        """`merge_record` reads empty as "no news", so this writes directly."""
        from juicebox.messaging.linkedin_flow import clear_target

        cleared = clear_target({"spec": {"job_titles": ["Investor"], "locations": ["sf"]}})
        assert cleared["spec"]["job_titles"] == []
        assert cleared["spec"]["locations"] == ["sf"], "only the target is dropped"


class TestRetargetEndToEnd:
    """The whole point: a rejection has to change what we search for."""

    def test_rejecting_the_target_asks_again_and_binds_the_answer(
        self, session, account, fake_channel
    ):
        from juicebox.messaging import linkedin_flow
        from juicebox.messaging.conversation import run_turn
        from juicebox.messaging.linkedin_flow import BATCH_RETARGET
        from juicebox.messaging.threads import record_inbound

        thread = _short_thread(session, account, fake_channel)
        thread.contact_email = "s@example.com"
        thread.contact_linkedin = "https://www.linkedin.com/in/sathyapanchu"
        thread.contact_name = "Sathya Panchu"
        thread.record = {
            "spec": {"job_titles": ["Investor"], "locations": ["San Francisco"]},
            "_linkedin": {"scrape": {"current_title": "Investor"}, "phase": "confirm"},
        }
        thread.last_field_asked = BATCH_CONFIRM
        session.flush()

        # They reject it, and the extractor (heuristic here, no key) misses it.
        record_inbound(session, thread=thread, body="No I want to switch to Strat fin",
                       provider_native_id="rt-1")
        result = run_turn(session, thread=thread)

        assert result.asked_field == BATCH_RETARGET, "must not hand over on a rejection"
        assert "instead" in fake_channel.bodies[-1].lower()

        # And the answer binds, even though nothing recognises "Strat fin".
        record_inbound(session, thread=thread, body="Strat fin", provider_native_id="rt-2")
        run_turn(session, thread=thread)
        session.refresh(thread)
        titles = (thread.record.get("spec") or {}).get("job_titles") or []
        assert titles == ["Strat fin"], f"searched for {titles} after they said no"
        assert not linkedin_flow.target_unchanged(thread)

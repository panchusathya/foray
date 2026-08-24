"""LinkedIn-first text intake and Linq delivery/read receipts."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from test_messaging import LINQ_OUR_NUMBER, LINQ_WEBHOOK_SECRET, _say, linq_payload, post_linq

from juicebox.enrich.linkedin_scrape import LinkedInProfile, set_scraper
from juicebox.jobs import KIND_LINKEDIN_SCRAPE
from juicebox.messaging.channels import WEB, set_channel
from juicebox.messaging.conversation import (
    ACTION_ASKED,
    ACTION_COMPLETED,
    ACTION_GREETED,
    ACTION_HELD,
    run_turn,
)
from juicebox.messaging.identity import (
    CONTACT_EMAIL,
    CONTACT_LINKEDIN,
    find_linkedin,
    link_candidate,
)
from juicebox.messaging.linkedin_flow import (
    BATCH_CONFIRM,
    BATCH_GAP_FILL,
    FLOW_LINKEDIN_SHORT,
    confirm_message,
)
from juicebox.messaging.prompts import lowercase_outbound, valid_question
from juicebox.messaging.threads import STATUS_COMPLETE, open_thread
from juicebox.models import Candidate, IntakeMessage, IntakeNumber, IntakeThread, Job
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
    def test_intro_asks_for_email_in_one_lowercase_message(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        result = run_turn(session, thread=thread)

        assert result.action == ACTION_GREETED
        assert result.asked_field == CONTACT_EMAIL
        assert len(fake_channel.bodies) == 1
        body = fake_channel.bodies[0]
        assert body == body.lower() or "http" in body
        assert "email" in body
        assert "vivian" in body

    def test_after_email_it_asks_for_linkedin(self, session, account, fake_channel):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        result = _say(session, thread, "ada@lovelace.dev")

        assert result.action == ACTION_ASKED
        assert result.asked_field == CONTACT_LINKEDIN
        assert "linkedin" in fake_channel.bodies[-1]

    def test_a_url_enqueues_a_scrape_and_holds(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        result = _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")

        assert result.action == ACTION_HELD
        assert thread.contact_linkedin == "https://www.linkedin.com/in/ada-lovelace"
        jobs = session.scalars(select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE)).all()
        assert len(jobs) == 1

    def test_scrape_success_confirms_inferred_facts(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")
        result = _finish_scrape(session, thread)

        assert result.action == ACTION_ASKED
        assert result.asked_field == BATCH_CONFIRM
        body = fake_channel.bodies[-1]
        assert "software engineer" in body
        assert "stripe" in body
        assert thread.contact_name == "Ada Lovelace"
        assert thread.record["spec"]["job_titles"] == ["Software Engineer"]

    def test_ic_gap_fill_asks_years_not_level(self, session, account, fake_channel):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")
        _finish_scrape(session, thread)
        result = _say(session, thread, "yes that's right")

        assert result.asked_field == BATCH_GAP_FILL
        body = fake_channel.bodies[-1]
        assert "years of experience" in body
        assert "what level" not in body
        assert body.count("- ") <= 3
        assert valid_question(body, batched=True)

    def test_gap_fill_answer_maps_years_and_wraps_up(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        run_turn(session, thread=thread)
        _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")
        _finish_scrape(session, thread)
        _say(session, thread, "yes that's right")
        result = _say(
            session,
            thread,
            "8 years. targeting 240k. i can start in two weeks. us citizen. yes you can share.",
        )

        assert result.action == ACTION_COMPLETED
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

    def test_a_first_inbound_url_still_prompts_for_email_first(
        self, session, account, fake_channel
    ):
        thread = _short_thread(session, account, fake_channel)
        result = _say(session, thread, "linkedin.com/in/ada-lovelace")
        assert result.action == ACTION_GREETED
        assert result.asked_field == CONTACT_EMAIL
        assert "email" in fake_channel.bodies[-1]
        assert thread.contact_linkedin.endswith("/ada-lovelace")


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

    def test_find_linkedin_reads_a_url_from_any_message(self):
        assert find_linkedin("sure https://linkedin.com/in/ada-lovelace works")


class TestLinkedinCopy:
    def test_confirm_and_gap_fill_are_lowercase_batches(self):
        profile = _profile()
        confirm = lowercase_outbound(confirm_message(profile))
        assert "software engineer" in confirm
        assert valid_question(confirm, batched=True)

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
            select(IntakeMessage).where(
                IntakeMessage.thread_id == thread.id, IntakeMessage.direction == "outbound"
            )
        ).one()
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

    def test_an_orphan_status_event_is_acknowledged(
        self, client, linq_number
    ):
        payload = {
            "event_type": "message.read",
            "event_id": "evt-orphan",
            "data": {"id": "msg-unknown", "read_at": "2026-02-05T19:13:58.177Z"},
        }
        response = post_linq(client, payload)
        assert response.status_code == 200
        assert response.json()["reason"] == "unknown_message"

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
        outbound = session.scalars(select(IntakeMessage)).one()
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

    def test_a_finished_chat_opens_a_new_thread_on_the_next_text(
        self, client, session, linq_number
    ):
        first = post_linq(client, linq_payload())
        assert first.status_code == 200
        original = session.scalars(select(IntakeThread)).one()
        original.status = STATUS_COMPLETE
        session.commit()

        again = post_linq(
            client,
            linq_payload(
                message={
                    "id": "msg-restart",
                    "parts": [{"type": "text", "value": "hey"}],
                }
            ),
        )
        assert again.status_code == 200
        threads = session.scalars(select(IntakeThread).order_by(IntakeThread.created_at)).all()
        assert len(threads) == 2
        assert threads[1].id != original.id
        assert threads[1].status == "active"
        assert again.json()["thread_id"] == threads[1].id

"""Connect-your-apps onboarding: the offer, the OAuth surface, the index job.

Everything here runs against ``FakeConnector`` — no OAuth app, no network, no
model key — which is the point: the flow has to be demonstrable offline the
same way the rest of intake is.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from juicebox.connectors import (
    ConnectorGrant,
    ConnectorProfile,
    ResumeHit,
    reset_connectors,
    set_connector,
)
from juicebox.connectors.extract import rank_hits
from juicebox.connectors.fake import FakeConnector, hit
from juicebox.docgen.render import render_resume_pdf
from juicebox.jobs import KIND_CONNECTOR_INDEX, KIND_INTAKE_TURN, KIND_LINKEDIN_SCRAPE
from juicebox.messaging.channels import WEB
from juicebox.messaging.connect_flow import BATCH_CONNECT
from juicebox.messaging.connect_links import mint_connect_link
from juicebox.messaging.conversation import ACTION_ASKED, ACTION_HELD, run_turn
from juicebox.messaging.identity import CONTACT_LINKEDIN
from juicebox.messaging.linkedin_flow import (
    BATCH_CONFIRM,
    BATCH_GAP_FILL,
    FLOW_LINKEDIN_SHORT,
    flow_meta,
)
from juicebox.messaging.prompts import valid_question
from juicebox.messaging.threads import open_thread, record_inbound
from juicebox.models import (
    CandidateConnection,
    CandidateDocument,
    ConsentRecord,
    IntakeThread,
    Job,
)
from juicebox.worker import process_job

RESUME_V1 = {
    "contact": {
        "name": "Ada Lovelace",
        "email": "ada@lovelace.dev",
        "phone": "",
        "location": "San Francisco, CA",
        "linkedin_url": "https://linkedin.com/in/ada",
        "github_url": "",
        "website_url": "",
    },
    "summary": "Software engineer.",
    "experience": [
        {
            "company": "Stripe",
            "title": "Software Engineer",
            "start": "2019-01",
            "end": "present",
            "bullets": ["Payments infrastructure."],
        }
    ],
    "education": [],
    "skills": ["python"],
}

PROFILE = ConnectorProfile(
    full_name="Ada Lovelace",
    current_title="Software Engineer",
    current_employer="Stripe",
    location="San Francisco",
    experience_years=8.0,
    skills=("Python", "Rust"),
    github_url="https://github.com/ada",
)


@pytest.fixture
def connector():
    fake = FakeConnector(profile=PROFILE)
    set_connector("fake", fake)
    yield fake
    reset_connectors()


def _short_thread(session, account, **kwargs):
    return open_thread(
        session,
        org_id=account["org_id"],
        channel=WEB,
        consent_messaging=True,
        flow=FLOW_LINKEDIN_SHORT,
        **kwargs,
    )


def _say(session, thread, body, *, native_id=""):
    record_inbound(session, thread=thread, body=body, provider_native_id=native_id)
    return run_turn(session, thread=thread)


def _to_offer(session, account, fake_channel):
    """The intro turn: the greeting bubble, then the profile ask riding it.

    The offer (or the plain LinkedIn/resume ask) now arrives with the intro —
    the email question that used to sit between them is gone; the OAuth
    callback delivers a verified address, and manual threads pick one up in
    the gap-fill.
    """
    thread = _short_thread(session, account)
    result = run_turn(session, thread=thread)
    return thread, result


def _connect(client, session, thread, *, code="ok"):
    """Drive the real OAuth surface end to end: page -> start -> callback."""
    from urllib.parse import urlparse

    link = mint_connect_link(thread)
    parsed = urlparse(link)
    path = f"{parsed.path}?{parsed.query}"
    page = client.get(path)
    assert page.status_code == 200
    start = client.get(
        path.replace(f"/v1/connect/{thread.id}", f"/v1/connect/{thread.id}/fake/start"),
        follow_redirects=False,
    )
    assert start.status_code == 302
    state = start.headers["location"].split("state=")[1]
    return client.get(f"/v1/connect/fake/callback?code={code}&state={state}")


def _index_job(session):
    return session.scalars(select(Job).where(Job.kind == KIND_CONNECTOR_INDEX)).one()


class TestOffer:
    def test_offer_replaces_the_profile_ask_when_configured(
        self, session, account, fake_channel, connector
    ):
        thread, result = _to_offer(session, account, fake_channel)
        assert result.action == ACTION_ASKED
        assert result.asked_field == BATCH_CONNECT
        body = fake_channel.bodies[-1]
        assert "/v1/connect/" in body
        # The alternative rides in the same message — declining costs no turn.
        assert "linkedin" in body.lower()
        assert valid_question(body, batched=True)
        assert flow_meta(thread).get("connect_offered") is True

    def test_no_connectors_configured_is_byte_identical(
        self, session, account, fake_channel
    ):
        _thread, result = _to_offer(session, account, fake_channel)
        assert result.asked_field == CONTACT_LINKEDIN
        assert "/v1/connect/" not in fake_channel.bodies[-1]

    def test_decline_falls_to_gap_fill_without_a_second_ask(
        self, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        result = _say(session, thread, "i'd rather just answer questions")
        assert result.asked_field == BATCH_GAP_FILL
        body = fake_channel.bodies[-1]
        assert "/v1/connect/" not in body
        # Moving on, not apologising: nothing failed.
        assert "no worries" not in body
        assert "couldn't" not in body

    def test_offer_is_never_repeated(self, session, account, fake_channel, connector):
        thread, _ = _to_offer(session, account, fake_channel)
        _say(session, thread, "hmm")
        _say(session, thread, "what?")
        offers = [b for b in fake_channel.bodies if "/v1/connect/" in b]
        assert len(offers) == 1

    def test_url_pasted_on_the_offer_turn_still_scrapes(
        self, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        result = _say(session, thread, "https://www.linkedin.com/in/ada-lovelace")
        assert result.action == ACTION_HELD
        assert (
            session.scalar(select(Job).where(Job.kind == KIND_LINKEDIN_SCRAPE)) is not None
        )

    def test_pending_index_holds_the_turn(self, session, account, fake_channel, connector):
        thread, _ = _to_offer(session, account, fake_channel)
        session.add(
            CandidateConnection(
                org_id=thread.org_id,
                thread_id=thread.id,
                provider="fake",
                index_state={"status": "running"},
            )
        )
        session.flush()
        result = _say(session, thread, "done, connected it")
        assert result.action == ACTION_HELD


class TestConnectRoutes:
    def test_page_needs_the_signed_token(self, client, session, account, connector):
        thread = _short_thread(session, account)
        session.commit()
        assert client.get(f"/v1/connect/{thread.id}?token=wrong").status_code == 401

    def test_callback_writes_connection_consent_and_job(
        self, client, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        thread.contact_email = ""  # the grant's verified address may fill it
        session.commit()
        response = _connect(client, session, thread)
        assert response.status_code == 200
        session.expire_all()

        connection = session.scalars(select(CandidateConnection)).one()
        assert connection.thread_id == thread.id
        assert connection.provider == "fake"
        assert connection.address == "fake@example.com"
        assert connection.token_blob  # encrypted, non-empty
        assert connection.index_state["status"] == "queued"

        consent = session.get(ConsentRecord, connection.consent_id)
        assert consent is not None
        assert consent.scope == "app_connect.fake"
        assert consent.disclosure_version == "connect-v1"
        assert "resume" in consent.disclosure_text

        refreshed = session.get(IntakeThread, thread.id)
        assert refreshed.contact_email == "fake@example.com"
        assert _index_job(session) is not None

    def test_callback_never_overwrites_a_stated_email(
        self, client, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        thread.contact_email = "ada@example.com"  # stated before the tap
        session.commit()
        _connect(client, session, thread)
        session.expire_all()
        assert session.get(IntakeThread, thread.id).contact_email == "ada@example.com"

    def test_reconnect_reuses_the_row_and_queues_a_fresh_run(
        self, client, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        session.commit()
        _connect(client, session, thread)
        _connect(client, session, thread)
        session.expire_all()
        assert len(session.scalars(select(CandidateConnection)).all()) == 1
        jobs = session.scalars(select(Job).where(Job.kind == KIND_CONNECTOR_INDEX)).all()
        assert len(jobs) == 2

    def test_disconnect_revokes_everything(
        self, client, session, account, fake_channel, connector
    ):
        thread, _ = _to_offer(session, account, fake_channel)
        session.commit()
        _connect(client, session, thread)
        link = mint_connect_link(thread)
        token = link.split("token=")[1]
        response = client.post(f"/v1/connect/{thread.id}/fake/disconnect?token={token}")
        assert response.status_code == 200
        session.expire_all()
        connection = session.scalars(select(CandidateConnection)).one()
        assert connection.status == "revoked"
        assert connection.token_blob == ""
        assert connector.revoked == [connection.id]
        consent = session.get(ConsentRecord, connection.consent_id)
        assert consent.revoked_at is not None


def _connected(client, session, account, fake_channel):
    thread, _ = _to_offer(session, account, fake_channel)
    session.commit()
    response = _connect(client, session, thread)
    assert response.status_code == 200
    return thread


class TestConnectorIndexJob:
    def test_profile_only_index_settles_and_confirms(
        self, client, session, account, fake_channel, connector
    ):
        thread = _connected(client, session, account, fake_channel)
        process_job(session, _index_job(session))
        session.expire_all()

        thread = session.get(IntakeThread, thread.id)
        scrape = flow_meta(thread).get("scrape") or {}
        assert scrape.get("current_title") == "Software Engineer"
        assert scrape.get("skills") == ["Python", "Rust"]
        connection = session.scalars(select(CandidateConnection)).one()
        assert connection.index_state["status"] == "done"

        result = run_turn(session, thread=thread)
        assert result.asked_field == BATCH_CONFIRM
        assert "software engineer" in fake_channel.bodies[-1]

    def test_resume_index_stores_the_document_and_only_the_parse_replies(
        self, client, session, account, fake_channel, connector
    ):
        connector.hits = [hit(filename="ada-resume.pdf")]
        connector.resume_bytes = render_resume_pdf(RESUME_V1)
        thread = _connected(client, session, account, fake_channel)
        # The callback wrote the verified email through the API's own session;
        # a real worker starts fresh, so drop this session's stale snapshot
        # before running the job (link_candidate needs that email now that the
        # intro no longer collects one).
        session.expire_all()
        process_job(session, _index_job(session))
        session.expire_all()

        doc = session.scalars(select(CandidateDocument)).one()
        assert doc.thread_id == thread.id
        assert doc.source == "connector"
        assert doc.kind == "resume_uploaded"
        # Exactly one consequence: the parse settles the thread, so the index
        # itself must not have queued a turn.
        assert (
            session.scalar(select(Job).where(Job.kind == KIND_INTAKE_TURN)) is None
        )

        parse = session.scalars(select(Job).where(Job.kind == "docs.parse")).one()
        from juicebox.llm.client import FakeTransport, LLMClient, set_llm

        set_llm(LLMClient(transport=FakeTransport(default=RESUME_V1), enabled=True))
        try:
            process_job(session, parse)
        finally:
            set_llm(None)
        session.expire_all()
        thread = session.get(IntakeThread, thread.id)
        assert (flow_meta(thread).get("scrape") or {}).get("current_title")
        result = run_turn(session, thread=thread)
        assert result.asked_field == BATCH_CONFIRM

    def test_failure_still_answers_with_gap_fill(
        self, client, session, account, fake_channel, connector
    ):
        connector.fail_index = "hits"
        thread = _connected(client, session, account, fake_channel)
        process_job(session, _index_job(session))
        session.expire_all()

        connection = session.scalars(select(CandidateConnection)).one()
        assert connection.index_state["status"] == "failed"
        thread = session.get(IntakeThread, thread.id)
        result = run_turn(session, thread=thread)
        assert result.asked_field == BATCH_GAP_FILL
        assert "couldn't pull much" in fake_channel.bodies[-1]

    def test_stopped_thread_is_never_indexed(
        self, client, session, account, fake_channel, connector
    ):
        thread = _connected(client, session, account, fake_channel)
        thread.status = "stopped"
        session.commit()
        before = len(fake_channel.bodies)
        process_job(session, _index_job(session))
        session.expire_all()
        connection = session.scalars(select(CandidateConnection)).one()
        assert connection.index_state["status"] == "failed"
        assert len(fake_channel.bodies) == before
        assert session.scalar(select(Job).where(Job.kind == KIND_INTAKE_TURN)) is None

    def test_managing_thread_keeps_its_confirm_slot(
        self, client, session, account, fake_channel, connector
    ):
        thread = _connected(client, session, account, fake_channel)
        thread.mode = "managing"
        session.commit()
        process_job(session, _index_job(session))
        session.expire_all()
        thread = session.get(IntakeThread, thread.id)
        assert not (flow_meta(thread).get("scrape") or {})
        # The conversation is still answered — through the manager, not confirm.
        assert (
            session.scalar(select(Job).where(Job.kind == KIND_INTAKE_TURN)) is not None
        )


class TestResumeSelection:
    def test_their_own_named_recent_file_wins(self):
        from datetime import UTC, datetime

        older = datetime(2024, 1, 1, tzinfo=UTC)
        newer = datetime(2025, 1, 1, tzinfo=UTC)
        hits = [
            hit(native_id="a", filename="notes.pdf", sent_by_owner=False, occurred_at=newer),
            hit(native_id="b", filename="ada-cv.pdf", sent_by_owner=True, occurred_at=older),
            hit(native_id="c", filename="ada-resume.pdf", sent_by_owner=True, occurred_at=newer),
            hit(native_id="d", filename="photo.png", sent_by_owner=True, occurred_at=newer),
        ]
        ranked = rank_hits(hits)
        assert [h.native_id for h in ranked] == ["c", "b", "a"]  # png never ranks


class TestNoContentIsStored:
    """The privacy contract, pinned structurally — the DiscoveredContact rule."""

    FORBIDDEN = ("body", "subject", "snippet", "preview", "text", "html")

    def test_connector_dtos_have_no_content_fields(self):
        for dto in (ResumeHit, ConnectorProfile, ConnectorGrant):
            for name in dto.__dataclass_fields__:
                assert not any(word in name.lower() for word in self.FORBIDDEN), (
                    f"{dto.__name__}.{name} looks like message content"
                )

    def test_connection_table_has_no_content_columns(self):
        for column in CandidateConnection.__table__.columns:
            assert not any(word in column.name.lower() for word in self.FORBIDDEN), (
                f"candidate_connections.{column.name} looks like message content"
            )

"""Reply drafts: generation, Graph draft creation, notification, worker wiring.

The feature ships **off** (``REPLY_DRAFTING_ENABLED``), so this module turns it
on explicitly. That is deliberate rather than incidental: without it
``test_no_job_for_opt_out_replies`` would pass for the wrong reason -- zero
jobs because the feature is disabled, not because the opt-out was respected.
``TestKillSwitch`` covers the off state.
"""

import pytest
from test_mailbox_sync import (
    FakeDeltaClient,
    _inbound,
    graph_provider,
    install_graph,
    install_graph_sender,
)

from juicebox.integrations.crypto import encrypt_json
from juicebox.integrations.reply_drafting import create_reply_draft, generate_reply_body
from juicebox.integrations.sync import sync_mailbox
from juicebox.jobs import KIND_MAILBOX_NOTIFY
from juicebox.models import Job, MailboxConnection, Message, ReplyDraft
from juicebox.outreach.sequencer import enroll_candidates, run_due
from juicebox.worker import process_job


@pytest.fixture(autouse=True)
def drafting_enabled(monkeypatch):
    """Every test below exercises the feature, so switch it on."""
    from juicebox.config import reset_settings_cache

    monkeypatch.setenv("REPLY_DRAFTING_ENABLED", "true")
    reset_settings_cache()
    yield
    reset_settings_cache()


class FakeDraftClient(FakeDeltaClient):
    """Delta client plus the drafting surface: createReply and notification send."""

    def __init__(self):
        super().__init__()
        self.reply_drafts: list[tuple[str, str]] = []  # (message_id, comment_html)
        self.sent_messages: list[dict] = []

    def create_reply_draft(self, message_id, comment_html):
        self.reply_drafts.append((message_id, comment_html))
        return {
            "id": f"draft-{len(self.reply_drafts)}",
            "webLink": f"https://outlook.office.com/mail/id/draft-{len(self.reply_drafts)}",
        }

    def create_draft(self, message):
        self.sent_messages.append(message)
        return {"id": f"note-{len(self.sent_messages)}"}

    def send_draft(self, message_id):
        self.sent_ids.append(message_id)


@pytest.fixture
def replied_org(session, account, outreach_fixture):
    """Mailbox + sequence sent via Graph + Ada's reply recorded through sync."""
    connection = MailboxConnection(
        org_id=account["org_id"],
        address="recruiter@goforay.io",
        token_blob=encrypt_json({"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}),
        inbox_delta_link="https://graph.test/delta?token=init",
    )
    session.add(connection)
    session.flush()
    sequence = outreach_fixture["sequence"]
    sequence.mailbox_id = connection.id
    install_graph_sender()
    ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
    enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id)
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()

    fake = FakeDraftClient()
    fake.queue(_inbound(body="This sounds interesting — happy to chat this week."))
    sync_mailbox(session, connection, provider=graph_provider(lambda db, conn: fake))
    session.commit()
    inbound = session.query(Message).filter_by(direction="inbound").one()
    return {"connection": connection, "inbound": inbound, "ada": ada, "fake": fake}


class TestGeneration:
    def test_opt_out_and_automation_get_no_draft(self, session, replied_org):
        inbound = replied_org["inbound"]
        inbound.reply_intent = "unsubscribe"
        assert (
            create_reply_draft(
                session,
                inbound=inbound,
                connection=replied_org["connection"],
                provider=graph_provider(lambda db, conn: replied_org["fake"]),
            )
            is None
        )


class TestCreateReplyDraft:
    def test_draft_lands_on_the_thread_with_a_notification(self, session, replied_org):
        fake = replied_org["fake"]
        # Fresh recorder so the outbox holds only the notification, not the
        # outreach step the fixture already sent.
        notifier = install_graph_sender()
        draft = create_reply_draft(
            session,
            inbound=replied_org["inbound"],
            connection=replied_org["connection"],
            provider=graph_provider(lambda db, conn: fake),
        )
        session.commit()

        assert draft is not None and draft.generated_by == "template"
        # The Graph draft was created as a reply on the inbound message's thread.
        [(thread_message_id, comment_html)] = fake.reply_drafts
        assert thread_message_id == replied_org["inbound"].provider_native_id
        assert "Ada" in comment_html
        assert draft.provider_draft_id == "draft-1"
        assert draft.web_link.endswith("draft-1")

        # The user got a "draft ready" email from their own mailbox to
        # themselves -- queued, so it waits out the gap between emails.
        assert notifier.outbox == []
        [job] = session.query(Job).filter(Job.kind == KIND_MAILBOX_NOTIFY).all()
        process_job(session, job)
        [notification] = notifier.outbox
        assert notification.to == "recruiter@goforay.io"
        assert notification.from_email == "recruiter@goforay.io"
        assert "Ada Okonkwo replied" in notification.subject
        assert "draft-1" in notification.html_body

    def test_idempotent_per_inbound_message(self, session, replied_org):
        fake = replied_org["fake"]
        first = create_reply_draft(
            session, inbound=replied_org["inbound"], connection=replied_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        second = create_reply_draft(
            session, inbound=replied_org["inbound"], connection=replied_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        assert first.id == second.id
        assert len(fake.reply_drafts) == 1
        assert session.query(ReplyDraft).count() == 1

class TestWorkerFlow:
    def test_sync_enqueues_and_worker_creates_the_draft(self, session, replied_org, monkeypatch):
        from juicebox.jobs import KIND_REPLY_DRAFT
        from juicebox.models import Job
        from juicebox.worker import process_job

        job = session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).one()
        assert job.payload["inbound_message_id"] == replied_org["inbound"].id

        fake = replied_org["fake"]
        install_graph(lambda db, conn: fake)
        process_job(session, job)
        session.commit()
        assert job.status == "succeeded"
        assert session.query(ReplyDraft).count() == 1

    def test_no_job_for_opt_out_replies(self, session, account, outreach_fixture):
        from juicebox.jobs import KIND_REPLY_DRAFT
        from juicebox.models import Job

        connection = MailboxConnection(
            org_id=account["org_id"],
            address="recruiter@goforay.io",
            inbox_delta_link="https://graph.test/delta?token=init",
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id
        install_graph_sender()
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()

        fake = FakeDraftClient()
        fake.queue(_inbound(body="Please stop contacting me.", graph_id="in-9"))
        sync_mailbox(session, connection, provider=graph_provider(lambda db, conn: fake))
        session.commit()
        assert session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).count() == 0


class TestKillSwitch:
    """The feature ships off, and off means off at both gates.

    One gate is not enough. Stopping only the enqueue leaves jobs that were
    queued before the switch was thrown, and those would still write into a
    mailbox we do not own. Stopping only the worker leaves a queue filling up
    with work nobody wants.
    """

    # No class-level autouse fixture on purpose: it would run before
    # ``replied_org``, so the stale-job test would have no queued job to
    # process and would pass without testing anything. Each test throws the
    # switch at the moment that matters instead.

    @staticmethod
    def _set(monkeypatch, value):
        from juicebox.config import reset_settings_cache

        monkeypatch.setenv("REPLY_DRAFTING_ENABLED", value)
        reset_settings_cache()

    def test_off_by_default(self, monkeypatch):
        """No env var set at all: the feature is still off."""
        from juicebox.config import get_settings, reset_settings_cache

        monkeypatch.delenv("REPLY_DRAFTING_ENABLED", raising=False)
        reset_settings_cache()
        assert get_settings().reply_drafting_enabled is False

    def test_a_reply_queues_nothing(self, session, account, outreach_fixture, monkeypatch):
        from juicebox.jobs import KIND_REPLY_DRAFT
        from juicebox.models import Job

        connection = MailboxConnection(
            org_id=account["org_id"],
            address="recruiter@goforay.io",
            inbox_delta_link="https://graph.test/delta?token=init",
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id
        install_graph_sender()
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()

        self._set(monkeypatch, "false")
        fake = FakeDraftClient()
        fake.queue(_inbound(body="Sounds interesting, tell me more.", graph_id="in-off"))
        sync_mailbox(session, connection, provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).count() == 0
        assert fake.reply_drafts == [], "nothing may be written into the mailbox"
        assert session.query(ReplyDraft).count() == 0

    def test_a_job_queued_before_the_switch_does_not_run(self, session, replied_org, monkeypatch):
        """Queued while enabled, processed after: it must not draft."""
        from juicebox.jobs import KIND_REPLY_DRAFT
        from juicebox.models import Job
        from juicebox.worker import process_job

        # The replied_org fixture ran with the module fixture on, so a job
        # exists -- exactly the state a real deployment is in at the moment
        # somebody switches the feature off.
        job = session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).one()

        self._set(monkeypatch, "false")
        fake = replied_org["fake"]
        install_graph(lambda db, conn: fake)
        process_job(session, job)
        session.commit()

        assert session.query(ReplyDraft).count() == 0
        assert fake.reply_drafts == [], "a stale job must not write into the mailbox"
        assert fake.sent_messages == [], "and must not email the owner either"


class TestPromptScoping:
    """What the model is shown when the reply has no sequence behind it."""

    def _stub_llm(self):
        from juicebox.llm.client import FakeTransport, LLMClient, set_llm

        transport = FakeTransport(default={"body": "Great — when are you free?"})
        set_llm(LLMClient(transport=transport, enabled=True))
        return transport

    def _prompt(self, transport) -> str:
        return "".join(
            str(message.get("content", ""))
            for message in transport.calls[0]["messages"]
        )

    def _hand_sent(self, session, *, org_id, candidate_id, body, **fields):
        from juicebox.models import utcnow

        row = Message(
            org_id=org_id,
            candidate_id=candidate_id,
            direction="outbound",
            source="microsoft",
            to_address="somebody@example.com",
            subject="Quick note",
            body=body,
            status="sent",
            sent_at=utcnow(),
            **fields,
        )
        session.add(row)
        session.flush()
        return row

    def _inbound_without_enrollment(self, session, *, org_id, candidate_id, **fields):
        from juicebox.models import utcnow

        row = Message(
            org_id=org_id,
            candidate_id=candidate_id,
            direction="inbound",
            source="microsoft",
            from_address="ada@example.com",
            to_address="recruiter@acme-talent.com",
            subject="Re: Quick note",
            body="Sounds interesting, tell me more.",
            status="received",
            reply_intent="interested",
            sent_at=utcnow(),
            **fields,
        )
        session.add(row)
        session.flush()
        return row

    def test_another_orgs_outbound_never_reaches_the_prompt(
        self, session, account, outreach_fixture
    ):
        """A thread-synced inbound has enrollment_id NULL, and filtering on it
        renders ``enrollment_id IS NULL`` -- which matched every hand-sent
        message in the database, whoever's tenant it belonged to."""
        from juicebox.models import Candidate, Org

        other = Org(name="Rival Search", slug="rival-search")
        session.add(other)
        session.flush()
        stranger = Candidate(
            org_id=other.id, full_name="Grace Hopper", source="manual", source_id="rival:1"
        )
        session.add(stranger)
        session.flush()
        self._hand_sent(
            session,
            org_id=other.id,
            candidate_id=stranger.id,
            body="RIVAL_CONFIDENTIAL_COPY",
        )

        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        inbound = self._inbound_without_enrollment(
            session, org_id=account["org_id"], candidate_id=ada.id
        )
        session.commit()

        transport = self._stub_llm()
        _, generated_by = generate_reply_body(session, inbound=inbound, candidate=ada)

        assert generated_by == "llm"
        assert "RIVAL_CONFIDENTIAL_COPY" not in self._prompt(transport)


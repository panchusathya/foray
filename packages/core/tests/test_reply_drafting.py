"""Reply drafts: generation, Graph draft creation, notification, worker wiring."""

import pytest
from test_mailbox_sync import FakeDeltaClient, _inbound

from juicebox.integrations.crypto import encrypt_json
from juicebox.integrations.reply_drafting import create_reply_draft, generate_reply_body
from juicebox.integrations.sync import sync_mailbox
from juicebox.models import MailboxConnection, Message, ReplyDraft
from juicebox.outreach.sender import SendResult, set_graph_sender_factory
from juicebox.outreach.sequencer import enroll_candidates, record_reply, run_due


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
    set_graph_sender_factory(
        lambda db, conn: type(
            "S",
            (),
            {
                "name": "graph",
                "send": lambda self, email: SendResult(
                    ok=True,
                    provider_message_id="<out-1@outlook.com>",
                    graph_message_id="out-1",
                    internet_message_id="<out-1@outlook.com>",
                    conversation_id="conv-1",
                ),
            },
        )()
    )
    ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
    enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id)
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()

    fake = FakeDraftClient()
    fake.queue(_inbound(body="This sounds interesting — happy to chat this week."))
    sync_mailbox(session, connection, client=fake)
    session.commit()
    inbound = session.query(Message).filter_by(direction="inbound").one()
    return {"connection": connection, "inbound": inbound, "ada": ada, "fake": fake}


class TestGeneration:
    def test_offline_template_uses_first_name_and_intent(self, session, replied_org):
        body, generated_by = generate_reply_body(
            session, inbound=replied_org["inbound"], candidate=replied_org["ada"]
        )
        assert generated_by == "template"
        assert "Ada" in body
        assert "call" in body.lower()

    def test_opt_out_and_automation_get_no_draft(self, session, replied_org):
        inbound = replied_org["inbound"]
        inbound.reply_intent = "unsubscribe"
        assert (
            create_reply_draft(
                session,
                inbound=inbound,
                connection=replied_org["connection"],
                client=replied_org["fake"],
            )
            is None
        )


class TestCreateReplyDraft:
    def test_draft_lands_on_the_thread_with_a_notification(self, session, replied_org):
        fake = replied_org["fake"]
        draft = create_reply_draft(
            session,
            inbound=replied_org["inbound"],
            connection=replied_org["connection"],
            client=fake,
        )
        session.commit()

        assert draft is not None and draft.generated_by == "template"
        # The Graph draft was created as a reply on the inbound message's thread.
        [(thread_message_id, comment_html)] = fake.reply_drafts
        assert thread_message_id == replied_org["inbound"].graph_message_id
        assert "Ada" in comment_html
        assert draft.graph_draft_id == "draft-1"
        assert draft.web_link.endswith("draft-1")

        # The user got a "draft ready" email from their own mailbox to themselves.
        [notification] = fake.sent_messages
        assert notification["toRecipients"] == [
            {"emailAddress": {"address": "recruiter@goforay.io"}}
        ]
        assert "Ada Okonkwo replied" in notification["subject"]
        assert "draft-1" in notification["body"]["content"]

    def test_idempotent_per_inbound_message(self, session, replied_org):
        fake = replied_org["fake"]
        first = create_reply_draft(
            session, inbound=replied_org["inbound"], connection=replied_org["connection"], client=fake
        )
        second = create_reply_draft(
            session, inbound=replied_org["inbound"], connection=replied_org["connection"], client=fake
        )
        assert first.id == second.id
        assert len(fake.reply_drafts) == 1
        assert session.query(ReplyDraft).count() == 1

    def test_webhook_replies_without_graph_ids_are_skipped(
        self, session, account, outreach_fixture, mailbox
    ):
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()
        outbound = session.query(Message).filter_by(direction="outbound").one()
        record_reply(session, message=outbound, subject="Re", body="Interested!")
        session.commit()
        inbound = session.query(Message).filter_by(direction="inbound").one()
        connection = MailboxConnection(org_id=account["org_id"], address="r@goforay.io")
        session.add(connection)
        session.flush()
        assert (
            create_reply_draft(
                session, inbound=inbound, connection=connection, client=FakeDraftClient()
            )
            is None
        )


class TestWorkerFlow:
    def test_sync_enqueues_and_worker_creates_the_draft(self, session, replied_org, monkeypatch):
        from juicebox.jobs import KIND_REPLY_DRAFT
        from juicebox.models import Job
        from juicebox.worker import process_job

        job = session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).one()
        assert job.payload["inbound_message_id"] == replied_org["inbound"].id

        fake = replied_org["fake"]
        monkeypatch.setattr(
            "juicebox.integrations.reply_drafting.GraphClient", lambda db, conn: fake
        )
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
        set_graph_sender_factory(
            lambda db, conn: type(
                "S",
                (),
                {
                    "name": "graph",
                    "send": lambda self, email: SendResult(ok=True, conversation_id="conv-1"),
                },
            )()
        )
        sequence.mailbox_id = connection.id
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        run_due(session, org_id=sequence.org_id)
        session.commit()

        fake = FakeDraftClient()
        fake.queue(_inbound(body="Please stop contacting me.", graph_id="in-9"))
        sync_mailbox(session, connection, client=fake)
        session.commit()
        assert session.query(Job).filter_by(kind=KIND_REPLY_DRAFT).count() == 0


class TestReplyDraftsApi:
    def test_list_reply_drafts(self, client, session, account, replied_org):
        create_reply_draft(
            session,
            inbound=replied_org["inbound"],
            connection=replied_org["connection"],
            client=replied_org["fake"],
        )
        session.commit()
        rows = client.get("/v1/reply-drafts", headers=account["headers"]).json()
        assert len(rows) == 1
        assert rows[0]["intent"] == "interested"
        assert rows[0]["generated_by"] == "template"
        assert rows[0]["web_link"].startswith("https://outlook.office.com")

"""Thread sync: manual Outlook mail entering the CRM, and its privacy guard.

The invariant defended here: nothing enters the database unless the mailbox
opted in AND the mail resolves to a candidate someone deliberately put into a
project, a sequence, or an existing thread. The recruiter's other
correspondence never lands, not even metadata.
"""

from __future__ import annotations

import pytest
from test_mailbox_sync import (
    FakeDeltaClient,
    _inbound,
    graph_provider,
    install_graph_sender,
)

from juicebox.integrations.crypto import encrypt_json
from juicebox.integrations.matching import build_address_index
from juicebox.integrations.sync import sync_mailbox
from juicebox.models import MailboxConnection, Message
from juicebox.outreach.sequencer import enroll_candidates, run_due


def _sent(
    *,
    to="ada@example.com",
    subject="Intro",
    body="Wrote this one by hand in Outlook.",
    conversation_id="conv-manual",
    graph_id="sent-1",
):
    return {
        "id": graph_id,
        "conversationId": conversation_id,
        "internetMessageId": f"<{graph_id}@outlook.com>",
        "from": {"emailAddress": {"address": "recruiter@goforay.io"}},
        "toRecipients": [{"emailAddress": {"address": to}}],
        "subject": subject,
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:120],
        "isDraft": False,
        "sentDateTime": "2026-08-18T09:00:00Z",
        "receivedDateTime": "2026-08-18T09:00:00Z",
        "hasAttachments": False,
        "webLink": "https://outlook.office.com/mail/deep-link",
    }


@pytest.fixture
def thread_org(session, account, outreach_fixture):
    """A connected, thread-sync-enabled mailbox; Ada enrolled and step 0 sent."""
    connection = MailboxConnection(
        org_id=account["org_id"],
        address="recruiter@goforay.io",
        token_blob=encrypt_json({"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}),
        inbox_delta_link="https://graph.test/delta?token=init",
        sent_delta_link="https://graph.test/sent-delta?token=init",
        thread_sync_enabled=True,
    )
    session.add(connection)
    session.flush()
    sequence = outreach_fixture["sequence"]
    sequence.mailbox_id = connection.id
    install_graph_sender()
    ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
    enroll_candidates(
        session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id
    )
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()
    return {"connection": connection, "sequence": sequence, "ada": ada}


class TestSentItemsIngest:
    def test_manual_mail_to_a_known_candidate_becomes_a_timeline_row(self, session, thread_org):
        fake = FakeDeltaClient()
        fake.queue_sent(_sent())
        summary = sync_mailbox(session, thread_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.sent_ingested == 1
        row = session.query(Message).filter_by(source="microsoft").one()
        assert row.enrollment_id is None
        assert row.direction == "outbound"
        assert row.candidate_id == thread_org["ada"].id
        assert row.from_address == "recruiter@goforay.io"
        assert row.to_address == "ada@example.com"
        assert row.web_link.startswith("https://outlook.office.com")
        assert row.sent_at is not None

    def test_mail_to_a_stranger_stores_nothing_at_all(self, session, thread_org):
        fake = FakeDeltaClient()
        fake.queue_sent(_sent(to="boss@westbridgecap.com", graph_id="sent-2"))
        summary = sync_mailbox(session, thread_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.sent_ingested == 0
        assert summary.unmatched_discarded >= 1
        assert session.query(Message).filter_by(source="microsoft").count() == 0

    def test_a_sequence_send_seen_in_sent_items_is_not_double_stored(self, session, thread_org):
        # The sequencer stored provider_native_id="out-1" at send time; the same
        # ImmutableId coming back through the Sent Items delta must be a no-op.
        before = session.query(Message).count()
        fake = FakeDeltaClient()
        fake.queue_sent(_sent(graph_id="out-1", conversation_id="conv-1"))
        summary = sync_mailbox(session, thread_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.sent_ingested == 0
        assert session.query(Message).count() == before

    def test_disabled_thread_sync_ingests_nothing(self, session, thread_org):
        connection = thread_org["connection"]
        connection.thread_sync_enabled = False
        session.commit()
        fake = FakeDeltaClient()
        fake.queue_sent(_sent(graph_id="sent-3"))
        summary = sync_mailbox(session, connection, provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.sent_ingested == 0
        assert session.query(Message).filter_by(source="microsoft").count() == 0

    def test_second_pass_over_the_same_message_is_a_noop(self, session, thread_org):
        for _ in range(2):
            fake = FakeDeltaClient()
            fake.queue_sent(_sent(graph_id="sent-4"))
            sync_mailbox(
                session,
                thread_org["connection"],
                provider=graph_provider(lambda db, conn, client=fake: client),
            )
            session.commit()
        assert session.query(Message).filter_by(provider_native_id="sent-4").count() == 1


class TestUnmatchedInbound:
    def test_inbound_from_a_known_candidate_without_an_outbound_is_kept(
        self, session, thread_org
    ):
        # A different conversation than anything we sent: no outbound match,
        # but the sender is a known candidate, so it belongs in their timeline.
        fake = FakeDeltaClient()
        fake.queue(
            _inbound(conversation_id="conv-other", graph_id="in-9", subject="Hey, question")
        )
        summary = sync_mailbox(session, thread_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        # conv-1 exists from the sequence send, so _match_outbound would match
        # by sender fallback... assert on what actually got stored instead:
        stored = session.query(Message).filter_by(provider_native_id="in-9").one()
        assert stored.candidate_id == thread_org["ada"].id
        assert summary.seen == 1

    def test_inbound_from_a_stranger_is_discarded_without_a_trace(self, session, thread_org):
        fake = FakeDeltaClient()
        fake.queue(
            _inbound(
                sender="newsletter@vendor.example",
                conversation_id="conv-x",
                graph_id="in-10",
            )
        )
        summary = sync_mailbox(session, thread_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.unmatched_discarded == 1
        assert session.query(Message).filter_by(provider_native_id="in-10").count() == 0


class TestScopeGuard:
    def test_a_candidate_with_no_project_sequence_or_thread_is_out_of_scope(
        self, session, account, outreach_fixture
    ):
        """Having a matching address is not enough — the candidate must have
        been deliberately engaged."""
        from juicebox.repository import upsert_candidates

        upsert_candidates(
            session,
            org_id=account["org_id"],
            payloads=[
                {
                    "source": "test",
                    "source_id": "bystander",
                    "full_name": "By Stander",
                    "emails": ["bystander@example.com"],
                }
            ],
        )
        session.commit()
        index = build_address_index(session, org_id=account["org_id"])
        assert "bystander@example.com" not in index

    def test_enrolled_candidates_are_in_scope(self, session, thread_org):
        index = build_address_index(session, org_id=thread_org["connection"].org_id)
        assert index.get("ada@example.com") == thread_org["ada"].id

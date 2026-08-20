"""Inbox sync: reply detection, matching, idempotency, worker wiring.

The fake lives at the GraphClient level rather than the provider level so these
tests keep exercising the real Graph-payload-to-MailboxEvent adapter, which is
where a field-name mistake would otherwise hide.
"""

import pytest

from juicebox.integrations import microsoft
from juicebox.integrations.crypto import encrypt_json
from juicebox.integrations.microsoft import MicrosoftProvider
from juicebox.integrations.providers import set_provider
from juicebox.integrations.sync import sync_mailbox
from juicebox.models import Enrollment, MailboxConnection, Message
from juicebox.outreach.sender import SendResult, set_sender_factory
from juicebox.outreach.sequencer import enroll_candidates, run_due
from juicebox.outreach.suppressions import is_suppressed


class FakeDeltaClient:
    """Stands in for GraphClient: hands out queued delta pages per folder."""

    def __init__(self):
        self.batches: list[list[dict]] = []
        self.sent_batches: list[list[dict]] = []
        self.baseline_calls = 0
        self.sent_baseline_calls = 0
        self.delta_calls = 0
        self.sent_delta_calls = 0
        self.searches: list[str] = []
        self.search_results: list[dict] = []
        self.reply_drafts: list[tuple[str, str]] = []

    def queue(self, *messages: dict) -> None:
        self.batches.append(list(messages))

    def queue_sent(self, *messages: dict) -> None:
        self.sent_batches.append(list(messages))

    def inbox_delta_baseline(self) -> str:
        self.baseline_calls += 1
        return "https://graph.test/delta?token=0"

    def sent_delta_baseline(self) -> str:
        self.sent_baseline_calls += 1
        return "https://graph.test/sent-delta?token=0"

    def inbox_delta(self, delta_link: str) -> tuple[list[dict], str]:
        self.delta_calls += 1
        batch = self.batches.pop(0) if self.batches else []
        return batch, f"https://graph.test/delta?token={self.delta_calls}"

    def sent_delta(self, delta_link: str) -> tuple[list[dict], str]:
        self.sent_delta_calls += 1
        batch = self.sent_batches.pop(0) if self.sent_batches else []
        return batch, f"https://graph.test/sent-delta?token={self.sent_delta_calls}"

    def search_participant(self, address: str, *, top: int = 25) -> list[dict]:
        self.searches.append(address)
        return list(self.search_results)

    def create_reply_draft(self, message_id: str, comment_html: str) -> dict:
        self.reply_drafts.append((message_id, comment_html))
        return {"id": "draft-1", "webLink": "https://outlook.test/draft-1"}


def graph_provider(client_factory) -> MicrosoftProvider:
    """A microsoft provider whose GraphClient is the given fake."""
    return MicrosoftProvider(client_factory=client_factory)


def install_graph(client_factory) -> MicrosoftProvider:
    """Register the fake-backed provider so worker and API paths pick it up."""
    provider = graph_provider(client_factory)
    set_provider("microsoft", provider)
    return provider


class RecordingSender:
    """A fake EmailSender that keeps every OutboundEmail it was handed.

    Everything a provider sends now goes through this seam -- outreach steps
    and the "your draft is ready" notification alike -- so the outbox is where
    both are asserted.
    """

    def __init__(
        self,
        *,
        name="graph",
        native_id="out-1",
        rfc_id="<out-1@outlook.com>",
        conversation_id="conv-1",
    ):
        self.name = name
        self.outbox = []
        self._native_id = native_id
        self._rfc_id = rfc_id
        self._conversation_id = conversation_id

    def send(self, email):
        self.outbox.append(email)
        return SendResult(
            ok=True,
            provider_message_id=self._rfc_id,
            provider_native_id=self._native_id,
            internet_message_id=self._rfc_id,
            conversation_id=self._conversation_id,
        )


def install_graph_sender(**kwargs) -> RecordingSender:
    """Register a fake microsoft sender reporting the ids a real send would."""
    sender = RecordingSender(**kwargs)
    set_sender_factory("microsoft", lambda db, conn: sender)
    return sender


def _inbound(
    *,
    sender="ada@example.com",
    subject="Re: role",
    body="Happy to chat next week!",
    conversation_id="conv-1",
    graph_id="in-1",
):
    return {
        "id": graph_id,
        "conversationId": conversation_id,
        "internetMessageId": f"<{graph_id}@example.com>",
        "from": {"emailAddress": {"address": sender}},
        "subject": subject,
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:120],
        "isDraft": False,
        "receivedDateTime": "2026-08-18T10:00:00Z",
    }


@pytest.fixture
def synced_org(session, account, outreach_fixture):
    """A connected mailbox that has sent step 0 to Ada via Graph."""
    connection = MailboxConnection(
        org_id=account["org_id"],
        address="recruiter@goforay.io",
        token_blob=encrypt_json({"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}),
        inbox_delta_link="https://graph.test/delta?token=init",
    )
    session.add(connection)
    session.flush()
    sequence = outreach_fixture["sequence"]
    sequence.sender_mode = "fixed"
    sequence.mailbox_id = connection.id

    install_graph_sender()
    ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
    enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id], project_id=sequence.project_id)
    session.commit()
    run_due(session, org_id=sequence.org_id)
    session.commit()
    return {"connection": connection, "sequence": sequence, "ada": ada}


class TestSyncMailbox:
    def test_first_sync_initialises_without_reading_history(self, session, account):
        connection = MailboxConnection(org_id=account["org_id"], address="r@goforay.io")
        session.add(connection)
        session.flush()
        fake = FakeDeltaClient()
        summary = sync_mailbox(
            session, connection, provider=graph_provider(lambda db, conn: fake)
        )
        assert summary.initialized and fake.baseline_calls == 1 and fake.delta_calls == 0
        assert connection.inbox_delta_link == "https://graph.test/delta?token=0"
        assert connection.last_synced_at is not None

    def test_reply_is_matched_by_conversation_and_recorded(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound())
        summary = sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        session.commit()

        assert summary.matched == 1 and summary.intents == ["interested"]
        outbound = session.query(Message).filter_by(direction="outbound").one()
        assert outbound.reply_intent == "interested"
        inbound = session.query(Message).filter_by(direction="inbound").one()
        assert inbound.provider_native_id == "in-1"
        assert inbound.mailbox_id == synced_org["connection"].id
        assert session.query(Enrollment).one().status == "replied"
        # Delta cursor advanced.
        assert synced_org["connection"].inbox_delta_link.endswith("token=1")

    def test_same_email_is_never_recorded_twice(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound())
        fake.queue(_inbound())  # Graph re-delivers the same message next delta
        provider = graph_provider(lambda db, conn: fake)
        sync_mailbox(session, synced_org["connection"], provider=provider)
        summary = sync_mailbox(session, synced_org["connection"], provider=provider)
        session.commit()
        assert summary.matched == 0 and summary.skipped == 1
        assert session.query(Message).filter_by(direction="inbound").count() == 1

    def test_falls_back_to_address_match_without_a_conversation(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound(conversation_id="conv-other", graph_id="in-2"))
        summary = sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        assert summary.matched == 1

    def test_matches_on_in_reply_to_when_the_thread_id_is_wrong(self, session, synced_org):
        """A reply whose thread id we have never seen, from an address we have
        two candidates for, still lands on the right outbound row via its
        In-Reply-To header."""
        payload = _inbound(conversation_id="", graph_id="in-hdr")
        event = microsoft.to_event(payload)
        from dataclasses import replace

        from juicebox.integrations.sync import _match_outbound

        matched = _match_outbound(
            session,
            org_id=synced_org["connection"].org_id,
            event=replace(event, in_reply_to="<out-1@outlook.com>", from_address="stranger@x.io"),
        )
        assert matched is not None
        assert matched.internet_message_id == "<out-1@outlook.com>"

    def test_own_and_unmatched_mail_is_skipped(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(
            _inbound(sender="recruiter@goforay.io", graph_id="own-1"),
            _inbound(sender="totally.unrelated@example.net", conversation_id="", graph_id="in-3"),
            {"id": "gone-1", "@removed": {"reason": "deleted"}},
        )
        summary = sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        assert summary.matched == 0 and summary.skipped == 3

    def test_opt_out_reply_suppresses_via_sync(self, session, synced_org):
        fake = FakeDeltaClient()
        fake.queue(_inbound(body="Please stop contacting me.", graph_id="in-4"))
        summary = sync_mailbox(
            session, synced_org["connection"], provider=graph_provider(lambda db, conn: fake)
        )
        session.commit()
        assert summary.intents == ["unsubscribe"]
        assert is_suppressed(
            session, org_id=synced_org["connection"].org_id, email="ada@example.com"
        )


class TestWorkerWiring:
    def test_tick_is_scheduled_only_with_an_active_mailbox(self, session, account):
        from juicebox.jobs import KIND_MAILBOX_SYNC
        from juicebox.models import Job
        from juicebox.worker import ensure_ticks

        ensure_ticks(session)
        kinds = {job.kind for job in session.query(Job).all()}
        assert KIND_MAILBOX_SYNC not in kinds

        session.add(MailboxConnection(org_id=account["org_id"], address="r@goforay.io"))
        session.flush()
        ensure_ticks(session)
        kinds = {job.kind for job in session.query(Job).all()}
        assert KIND_MAILBOX_SYNC in kinds

    def test_the_dispatcher_covers_every_provider(self, session, synced_org):
        """A Zoho mailbox that never gets a job is a mailbox whose replies are
        never detected, so the fan-out must not filter by provider."""
        from juicebox.jobs import KIND_MAILBOX_SYNC, KIND_MAILBOX_SYNC_ONE, enqueue
        from juicebox.models import Job
        from juicebox.worker import process_job

        zoho_box = MailboxConnection(
            org_id=synced_org["connection"].org_id,
            address="z@goforay.io",
            provider="zoho",
        )
        session.add(zoho_box)
        session.flush()
        job = enqueue(
            session,
            org_id=synced_org["connection"].org_id,
            kind=KIND_MAILBOX_SYNC,
            idempotency_key="sync-dispatch-providers",
            payload={"org_id": synced_org["connection"].org_id},
        )
        process_job(session, job)
        session.commit()
        queued = session.query(Job).filter_by(kind=KIND_MAILBOX_SYNC_ONE).all()
        assert {j.payload["mailbox_id"] for j in queued} == {
            synced_org["connection"].id,
            zoho_box.id,
        }

    def test_dispatcher_fans_out_per_mailbox_and_reschedules(self, session, synced_org):
        """One job per mailbox, so a slow mailbox cannot starve the others."""
        from juicebox.jobs import KIND_MAILBOX_SYNC, KIND_MAILBOX_SYNC_ONE, enqueue
        from juicebox.models import Job
        from juicebox.worker import process_job

        session.add(
            MailboxConnection(
                org_id=synced_org["connection"].org_id, address="second@goforay.io"
            )
        )
        session.flush()
        job = enqueue(
            session,
            org_id=synced_org["connection"].org_id,
            kind=KIND_MAILBOX_SYNC,
            idempotency_key="sync-dispatch",
            payload={"org_id": synced_org["connection"].org_id},
        )
        process_job(session, job)
        session.commit()
        assert job.status == "succeeded"
        per_mailbox = session.query(Job).filter_by(kind=KIND_MAILBOX_SYNC_ONE).all()
        assert len(per_mailbox) == 2
        # The dispatcher keeps its own loop alive.
        assert session.query(Job).filter_by(kind=KIND_MAILBOX_SYNC, status="queued").count() == 1

    def test_per_mailbox_job_syncs_and_records(self, session, synced_org):
        from juicebox.jobs import KIND_MAILBOX_SYNC_ONE, enqueue
        from juicebox.worker import process_job

        fake = FakeDeltaClient()
        fake.queue(_inbound(graph_id="in-5"))
        install_graph(lambda db, conn: fake)
        job = enqueue(
            session,
            org_id=synced_org["connection"].org_id,
            kind=KIND_MAILBOX_SYNC_ONE,
            idempotency_key="sync-one-test",
            payload={
                "org_id": synced_org["connection"].org_id,
                "mailbox_id": synced_org["connection"].id,
            },
        )
        process_job(session, job)
        session.commit()
        assert job.status == "succeeded"
        assert session.query(Message).filter_by(direction="inbound").count() == 1

    def test_a_revoked_token_does_not_park_the_job(self, session, synced_org):
        """A broken mailbox records its error and succeeds; retrying forever
        would only fill the queue while the healthy mailboxes carry on."""
        from juicebox.jobs import KIND_MAILBOX_SYNC_ONE, enqueue
        from juicebox.worker import process_job

        def broken(db, connection):
            client = FakeDeltaClient()
            client.inbox_delta = lambda link: (_ for _ in ()).throw(
                microsoft.MicrosoftError("token revoked", status_code=401)
            )
            return client

        install_graph(broken)
        job = enqueue(
            session,
            org_id=synced_org["connection"].org_id,
            kind=KIND_MAILBOX_SYNC_ONE,
            idempotency_key="sync-one-broken",
            payload={
                "org_id": synced_org["connection"].org_id,
                "mailbox_id": synced_org["connection"].id,
            },
        )
        process_job(session, job)
        session.commit()
        assert job.status == "succeeded"
        assert "token revoked" in synced_org["connection"].error


class TestSyncApi:
    def test_sync_now_endpoint(self, client, session, account):
        connection = MailboxConnection(org_id=account["org_id"], address="r@goforay.io")
        session.add(connection)
        session.commit()
        install_graph(lambda db, conn: FakeDeltaClient())
        response = client.post(
            f"/v1/integrations/mailboxes/{connection.id}/sync", headers=account["headers"]
        )
        assert response.status_code == 200, response.text
        assert response.json()["initialized"] is True

        listed = client.get("/v1/integrations/mailboxes", headers=account["headers"]).json()
        assert listed["connections"][0]["last_synced_at"] is not None

    def test_sync_now_rejects_revoked_and_foreign(self, client, session, account):
        connection = MailboxConnection(
            org_id=account["org_id"], address="r@goforay.io", status="revoked"
        )
        session.add(connection)
        session.commit()
        assert (
            client.post(
                f"/v1/integrations/mailboxes/{connection.id}/sync", headers=account["headers"]
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/v1/integrations/mailboxes/mbx_nope/sync", headers=account["headers"]
            ).status_code
            == 404
        )

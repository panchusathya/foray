"""Style learning: Sent Items delta → draft outcomes → examples in the prompt."""

from test_mailbox_sync import graph_provider
from test_reply_drafting import replied_org  # noqa: F401 - fixture

from juicebox.integrations.reply_drafting import create_reply_draft
from juicebox.integrations.sync import strip_quoted_history, sync_mailbox
from juicebox.llm.client import FakeTransport, LLMClient, set_llm


def _sent_payload(*, graph_id, conversation_id="conv-1", body="Sounds good, let's talk Friday."):
    return {
        "id": graph_id,
        "conversationId": conversation_id,
        "from": {"emailAddress": {"address": "recruiter@goforay.io"}},
        "subject": "Re: role",
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:120],
        "isDraft": False,
        "receivedDateTime": "2026-08-18T12:00:00Z",
    }


class TestQuoteStripping:
    def test_common_separators_are_cut(self):
        for tail in (
            "\n\nFrom: Ada <ada@example.com>\nSent: Monday",
            "\n-----Original Message-----\nolder text",
            "\n________________________________\nolder",
            "\nOn Mon, Aug 17, 2026 at 9:00 AM Ada Okonkwo wrote:\n> hi",
        ):
            assert strip_quoted_history("My actual reply." + tail) == "My actual reply."

class TestSentItemsSync:
    def _drafted(self, session, replied_org):  # noqa: F811
        return create_reply_draft(
            session,
            inbound=replied_org["inbound"],
            connection=replied_org["connection"],
            provider=graph_provider(lambda db, conn: replied_org["fake"]),
        )

    def test_sending_our_draft_marks_it_sent_with_final_text(self, session, replied_org):  # noqa: F811
        draft = self._drafted(session, replied_org)
        fake = replied_org["fake"]
        # sent_delta_link was empty until now; first pass baselines it.
        sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        fake.queue_sent(
            _sent_payload(
                graph_id=draft.provider_draft_id,
                body="Great — Thursday 3pm works.\n\nFrom: Ada <ada@example.com>\nquoted",
            )
        )
        summary = sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.drafts_sent == 1
        session.refresh(draft)
        assert draft.status == "sent"
        assert draft.final_sent_text == "Great — Thursday 3pm works."
        assert draft.sent_at is not None

    def test_own_reply_on_the_thread_marks_the_draft_discarded(self, session, replied_org):  # noqa: F811
        draft = self._drafted(session, replied_org)
        fake = replied_org["fake"]
        sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        fake.queue_sent(
            _sent_payload(graph_id="totally-different", body="Wrote my own thing instead.")
        )
        summary = sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        session.commit()

        assert summary.drafts_discarded == 1
        session.refresh(draft)
        assert draft.status == "discarded"
        assert draft.final_sent_text == "Wrote my own thing instead."

    def test_unrelated_sent_mail_changes_nothing(self, session, replied_org):  # noqa: F811
        draft = self._drafted(session, replied_org)
        fake = replied_org["fake"]
        sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        fake.queue_sent(
            _sent_payload(graph_id="other", conversation_id="conv-unrelated", body="hello")
        )
        summary = sync_mailbox(session, replied_org["connection"], provider=graph_provider(lambda db, conn: fake))
        assert summary.drafts_sent == 0 and summary.drafts_discarded == 0
        session.refresh(draft)
        assert draft.status == "created"


class TestStyleExamples:
    def test_examples_reach_the_llm_prompt(self, session, replied_org):  # noqa: F811
        connection = replied_org["connection"]
        draft = create_reply_draft(
            session,
            inbound=replied_org["inbound"],
            connection=connection,
            provider=graph_provider(lambda db, conn: replied_org["fake"]),
        )
        draft.status = "sent"
        draft.final_sent_text = "VOICE-MARKER: cheers, T"
        draft.sent_at = draft.created_at
        session.flush()

        transport = FakeTransport([{"body": "drafted in your voice"}])
        set_llm(LLMClient(transport=transport, enabled=True, model="test-model"))
        try:
            from juicebox.integrations.reply_drafting import generate_reply_body

            body, generated_by = generate_reply_body(
                session,
                inbound=replied_org["inbound"],
                candidate=replied_org["ada"],
                mailbox_id=connection.id,
            )
        finally:
            set_llm(None)

        assert generated_by == "llm" and body == "drafted in your voice"
        prompt = transport.calls[0]["messages"][0]["content"]
        assert "<style_examples>" in prompt
        assert "VOICE-MARKER: cheers, T" in prompt

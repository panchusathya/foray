"""Drafted replies to ordinary inbox mail, held for review.

Two things these are really guarding. That the queue only ever fills with mail
a person actually has to answer -- fill it with receipts and nobody opens it.
And that nothing reaches the mailbox without an explicit approval, because the
trigger here is merely "somebody emailed you", which is not consent to write
into their Drafts folder.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from juicebox.config import get_settings, reset_settings_cache
from juicebox.integrations.inbound_filters import sender_looks_automated
from juicebox.integrations.inbox_drafting import (
    _InboundView,
    create_inbox_draft,
    generate_for_draft,
    place_in_mailbox,
)
from juicebox.integrations.providers import (
    DraftResult,
    ProviderError,
    set_provider,
)
from juicebox.models import InboxDraft, MailboxConnection


@pytest.fixture
def mailbox(session, account):
    connection = MailboxConnection(
        org_id=account["org_id"], address="recruiter@goforay.io", provider="microsoft"
    )
    session.add(connection)
    session.flush()
    return connection


class RecordingProvider:
    """Stands in for Graph/Zoho. Records what it was asked to place."""

    name = "microsoft"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def is_configured(self) -> bool:
        return True

    def place_reply_draft(self, db, *, connection, inbound, html_body: str) -> DraftResult:
        if self.fail:
            raise ProviderError("mailbox said no")
        self.calls.append(
            {
                "native_id": inbound.provider_native_id,
                "from_address": inbound.from_address,
                "to_address": inbound.to_address,
                "subject": inbound.subject,
                "body": inbound.body,
                "html": html_body,
            }
        )
        return DraftResult(provider_draft_id="draft-1", web_link="https://mail/draft-1")


@pytest.fixture
def recording(request):
    provider = RecordingProvider(fail=getattr(request, "param", False))
    set_provider("microsoft", provider)
    yield provider
    set_provider("microsoft", None)


@pytest.fixture
def enabled(monkeypatch):
    """Turn the feature on for one test. Off is the default everywhere else."""
    monkeypatch.setenv("INBOX_DRAFTING_ENABLED", "true")
    reset_settings_cache()
    yield
    monkeypatch.delenv("INBOX_DRAFTING_ENABLED", raising=False)
    reset_settings_cache()


def _draft(session, connection, **overrides):
    payload = {
        "org_id": connection.org_id,
        "inbound_native_id": "msg-1",
        "from_address": "sarah@acmerobotics.com",
        "subject": "Quick question",
        "inbound_body": "Do you have a moment to talk on Thursday?",
        "thread_id": "thread-1",
        "rfc_message_id": "<msg-1@acmerobotics.com>",
        "received_at": datetime(2026, 9, 15, 9, 0, tzinfo=UTC),
    }
    payload.update(overrides)
    return create_inbox_draft(session, connection=connection, **payload)


# --------------------------------------------------------------------------
# who gets a draft
# --------------------------------------------------------------------------


class TestSenderFilter:
    @pytest.mark.parametrize(
        "address",
        [
            "sarah@acmerobotics.com",
            "jane.doe@sequoiacap.com",
            "m.oconnell@some-startup.io",
        ],
    )
    def test_a_person_gets_through(self, address):
        assert not sender_looks_automated(from_address=address, subject="Re: intro")[0]

    @pytest.mark.parametrize(
        "address",
        [
            "no-reply@linkedin.com",
            "noreply+123@stripe.com",
            "notifications@github.com",
            "mailer-daemon@example.com",
            "bounces@news.sendgrid.net",
        ],
    )
    def test_a_robot_does_not(self, address):
        assert sender_looks_automated(from_address=address, subject="Hello")[0]

    def test_an_out_of_office_is_refused(self):
        assert sender_looks_automated(
            from_address="real.person@corp.com", subject="Automatic reply: Out of office"
        )[0]

    def test_a_calendar_response_is_refused(self):
        assert sender_looks_automated(
            from_address="real.person@corp.com", subject="Accepted: Intro call"
        )[0]

    def test_one_mention_of_unsubscribe_is_not_enough(self):
        """A marketing footer on a colleague's mail must not drop the mail.

        Refusing on a single keyword is how a real conversation disappears with
        no trace, which is the expensive direction to be wrong in.
        """
        automated, _ = sender_looks_automated(
            from_address="dave@startup.io",
            subject="Re: catching up",
            body="Good seeing you. (Ignore the unsubscribe link our CRM adds.)",
        )
        assert not automated

    def test_a_real_bulk_footer_is(self):
        automated, why = sender_looks_automated(
            from_address="team@company.com",
            subject="Our October update",
            body="Read more.\nUnsubscribe | manage your email preferences | "
            "view this email in your browser",
        )
        assert automated and why == "bulk-mail footer"

    def test_a_missing_address_is_refused(self):
        assert sender_looks_automated(from_address="")[0]


# --------------------------------------------------------------------------
# the row
# --------------------------------------------------------------------------


class TestCreate:
    def test_a_draft_starts_pending_and_unwritten(self, session, mailbox):
        draft = _draft(session, mailbox)
        assert draft.status == "pending"
        # The sync loop must not pay for a model call per message.
        assert draft.generated_by == ""
        assert draft.body == ""

    def test_the_same_message_twice_makes_one_draft(self, session, mailbox):
        assert _draft(session, mailbox) is not None
        assert _draft(session, mailbox) is None
        assert session.query(InboxDraft).count() == 1

    def test_a_stranger_needs_no_candidate(self, session, mailbox):
        draft = _draft(session, mailbox)
        assert draft.candidate_id is None
        assert draft.message_id is None


class TestGenerate:
    def test_the_offline_path_writes_something_sendable(self, session, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        assert draft.generated_by == "template"
        assert draft.body.strip()
        assert draft.generated_body == draft.body

    def test_a_retry_does_not_overwrite_an_edit(self, session, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        draft.body = "Thursday works. 3pm?"
        session.flush()
        generate_for_draft(session, draft=draft)
        assert draft.body == "Thursday works. 3pm?"


# --------------------------------------------------------------------------
# nothing reaches the mailbox without an approval
# --------------------------------------------------------------------------


class TestPlacement:
    def test_creating_a_draft_writes_nothing_to_the_mailbox(
        self, session, mailbox, recording
    ):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        assert recording.calls == []

    def test_approval_places_it(self, session, mailbox, recording):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        place_in_mailbox(session, draft=draft)
        assert draft.status == "placed"
        assert draft.provider_draft_id == "draft-1"
        assert len(recording.calls) == 1

    @pytest.mark.parametrize("recording", [True], indirect=True)
    def test_a_provider_failure_leaves_it_pending(self, session, mailbox, recording):
        """Marking it placed when nothing was written loses the reply silently."""
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        with pytest.raises(ProviderError):
            place_in_mailbox(session, draft=draft)
        assert draft.status == "pending"

    def test_an_inactive_mailbox_refuses(self, session, mailbox, recording):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        mailbox.status = "revoked"
        session.flush()
        with pytest.raises(ProviderError):
            place_in_mailbox(session, draft=draft)


class TestInboundView:
    def test_it_carries_every_field_zoho_reads(self, session, mailbox):
        """Graph replies by id alone; Zoho rebuilds the reply by hand.

        A field missing here is an AttributeError at the moment of writing to
        a live mailbox, on the provider that is harder to exercise in tests.
        """
        draft = _draft(session, mailbox)
        view = _InboundView(draft, mailbox)
        for attribute in (
            "provider_native_id",
            "conversation_id",
            "internet_message_id",
            "subject",
            "body",
            "body_html",
            "from_address",
            "to_address",
            "sent_at",
        ):
            assert hasattr(view, attribute), attribute
        assert view.from_address == "sarah@acmerobotics.com"
        # The reply goes out from the connected mailbox.
        assert view.to_address == mailbox.address


# --------------------------------------------------------------------------
# the API
# --------------------------------------------------------------------------


class TestApi:
    def test_the_queue_lists_pending_drafts(self, client, session, account, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        response = client.get("/v1/inbox-drafts", headers=account["headers"])
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["pending_count"] == 1
        assert payload["drafts"][0]["from_address"] == "sarah@acmerobotics.com"
        assert payload["drafts"][0]["subject"] == "Quick question"

    def test_editing_keeps_what_we_generated(self, client, session, account, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        response = client.patch(
            f"/v1/inbox-drafts/{draft.id}",
            json={"body": "Thursday works."},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["edited"] is True
        session.refresh(draft)
        assert draft.body == "Thursday works."
        assert draft.generated_body != draft.body

    def test_approving_an_empty_draft_is_refused(
        self, client, session, account, mailbox, recording
    ):
        draft = _draft(session, mailbox)
        draft.generated_by = "llm"  # the model declined to write this one
        session.commit()
        response = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
        )
        assert response.status_code == 422
        assert recording.calls == []

    def test_approving_sends_the_edited_text(
        self, client, session, account, mailbox, recording
    ):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        response = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve",
            json={"body": "Thursday at 3 works for me."},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "placed"
        assert "Thursday at 3 works" in recording.calls[0]["html"]

    def test_approving_twice_is_refused(self, client, session, account, mailbox, recording):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        first = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
        )
        assert second.status_code == 409
        assert len(recording.calls) == 1

    def test_dismissing_removes_it_from_the_queue(self, client, session, account, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        assert (
            client.post(
                f"/v1/inbox-drafts/{draft.id}/dismiss", headers=account["headers"]
            ).status_code
            == 200
        )
        listing = client.get("/v1/inbox-drafts", headers=account["headers"]).json()
        assert listing["pending_count"] == 0 and listing["drafts"] == []

    def test_a_dismissed_draft_cannot_be_approved(
        self, client, session, account, mailbox, recording
    ):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        client.post(f"/v1/inbox-drafts/{draft.id}/dismiss", headers=account["headers"])
        response = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
        )
        assert response.status_code == 409
        assert recording.calls == []

    def test_another_org_cannot_see_it(self, client, session, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        other = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
                "name": "Bo",
            },
        )
        headers = {"Authorization": f"Bearer {other.json()['access_token']}"}

        assert client.get("/v1/inbox-drafts", headers=headers).json()["drafts"] == []
        assert (
            client.post(f"/v1/inbox-drafts/{draft.id}/dismiss", headers=headers).status_code
            == 404
        )


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------


class TestKillSwitch:
    def test_off_by_default(self):
        reset_settings_cache()
        assert get_settings().inbox_drafting_enabled is False


class TestApproveBodyHandling:
    def test_approving_without_a_body_keeps_the_draft(
        self, client, session, account, mailbox, recording
    ):
        """Regression: an omitted body used to clear the draft.

        ``body`` defaulted to ``""``, so "send what you wrote" and "replace it
        with nothing" arrived identically and the generated text was wiped on
        the way to the mailbox.
        """
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        generated = draft.body
        session.commit()

        response = client.post(
            f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
        )
        assert response.status_code == 200, response.text
        session.refresh(draft)
        assert draft.body == generated
        assert generated.strip()
        # to_html splits on blank lines, so compare a phrase rather than the
        # whole body.
        assert "Let me come back to you" in recording.calls[0]["html"]

    def test_patching_nothing_is_refused(self, client, session, account, mailbox):
        draft = _draft(session, mailbox)
        generate_for_draft(session, draft=draft)
        session.commit()
        response = client.patch(
            f"/v1/inbox-drafts/{draft.id}", json={}, headers=account["headers"]
        )
        assert response.status_code == 422


class TestSyncGate:
    """What the mailbox sync decides to queue, before anything is written."""

    def _event(self, **overrides):
        from juicebox.integrations.providers import MailboxEvent

        payload = {
            "native_id": "evt-1",
            "from_address": "sarah@acmerobotics.com",
            "subject": "Quick question",
            "body": "Are you free Thursday?",
            "thread_id": "t-1",
            "rfc_message_id": "<evt-1@acme.com>",
        }
        payload.update(overrides)
        return MailboxEvent(**payload)

    def _enqueue(self, session, mailbox, event):
        from juicebox.integrations.sync import _enqueue_inbox_draft
        from juicebox.models import utcnow

        _enqueue_inbox_draft(
            session,
            connection=mailbox,
            event=event,
            candidate_id=None,
            message_id=None,
            now=utcnow(),
        )

    def test_nothing_is_queued_while_the_feature_is_off(self, session, mailbox):
        """The switch is checked before the row, so an org that has not opted
        in never stores mail from people who are not in its CRM."""
        self._enqueue(session, mailbox, self._event())
        assert session.query(InboxDraft).count() == 0

    def test_a_real_person_is_queued(self, session, mailbox, enabled):
        self._enqueue(session, mailbox, self._event())
        draft = session.query(InboxDraft).one()
        assert draft.from_address == "sarah@acmerobotics.com"
        assert draft.status == "pending"

    def test_a_robot_is_not(self, session, mailbox, enabled):
        self._enqueue(session, mailbox, self._event(from_address="no-reply@linkedin.com"))
        assert session.query(InboxDraft).count() == 0

    def test_our_own_mail_is_not(self, session, mailbox, enabled):
        """Mail we sent, seen again in a folder we read, is not correspondence."""
        self._enqueue(session, mailbox, self._event(from_address=mailbox.address.upper()))
        assert session.query(InboxDraft).count() == 0

    def test_the_quoted_history_is_stripped(self, session, mailbox, enabled):
        event = self._event(
            body="Thursday suits me.\n\nOn Mon, recruiter@goforay.io wrote:\n> the original"
        )
        self._enqueue(session, mailbox, event)
        draft = session.query(InboxDraft).one()
        assert "the original" not in draft.inbound_body
        assert "Thursday suits me." in draft.inbound_body


class TestProviderFailureTranslation:
    def test_a_bad_token_reads_as_a_provider_failure(self, session, mailbox):
        """The most ordinary failure there is: an expired or re-keyed mailbox.

        The crypto layer raises a bare ValueError, which used to escape as a
        500 saying "Internal Server Error" with nothing written to
        ``draft.error`` -- about a mailbox the user only needed to reconnect.
        """

        class Exploding:
            name = "microsoft"

            def is_configured(self) -> bool:
                return True

            def place_reply_draft(self, db, *, connection, inbound, html_body):
                raise ValueError("token blob is invalid or was encrypted with a different key")

        set_provider("microsoft", Exploding())
        try:
            draft = _draft(session, mailbox)
            generate_for_draft(session, draft=draft)
            with pytest.raises(ProviderError) as caught:
                place_in_mailbox(session, draft=draft)
            assert "token blob" in str(caught.value)
            assert draft.status == "pending"
        finally:
            set_provider("microsoft", None)

    def test_the_api_reports_it_as_a_mailbox_problem(self, client, session, account, mailbox):
        class Exploding:
            name = "microsoft"

            def is_configured(self) -> bool:
                return True

            def place_reply_draft(self, db, *, connection, inbound, html_body):
                raise ValueError("token blob is invalid")

        set_provider("microsoft", Exploding())
        try:
            draft = _draft(session, mailbox)
            generate_for_draft(session, draft=draft)
            session.commit()
            response = client.post(
                f"/v1/inbox-drafts/{draft.id}/approve", json={}, headers=account["headers"]
            )
            assert response.status_code == 502, response.text
            assert "mailbox" in response.json()["detail"].lower()
            session.refresh(draft)
            assert draft.status == "pending"
            assert draft.error
        finally:
            set_provider("microsoft", None)

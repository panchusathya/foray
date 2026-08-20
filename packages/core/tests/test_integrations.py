"""Mailbox integrations: OAuth connect, token storage, sends, sender resolution."""

import time

import pytest

from juicebox.integrations import microsoft
from juicebox.integrations.crypto import decrypt_json, encrypt_json
from juicebox.integrations.microsoft import MicrosoftProvider
from juicebox.integrations.providers import set_provider
from juicebox.models import MailboxConnection, Message
from juicebox.outreach.sender import (
    FailingSender,
    SendResult,
    resolve_sender,
    set_sender_factory,
)
from juicebox.outreach.sequencer import enroll_candidates, run_due


@pytest.fixture
def ms_configured(monkeypatch):
    monkeypatch.setenv("MS_CLIENT_ID", "client-123")
    monkeypatch.setenv("MS_CLIENT_SECRET", "secret-456")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


class _BaselineOnlyClient:
    """Just the delta-baseline surface the connect callback touches."""

    def __init__(self, db, connection):
        pass

    def inbox_delta_baseline(self):
        return "https://graph.test/delta?token=connect"

    def sent_delta_baseline(self):
        return "https://graph.test/sent-delta?token=connect"


def _provider_for(name: str, body: dict) -> dict:
    return next(entry for entry in body["providers"] if entry["name"] == name)


def _fake_exchange(monkeypatch, *, address="tushar@goforay.io", name="Tushar Gupta"):
    monkeypatch.setattr(
        microsoft,
        "exchange_code",
        lambda code: {
            "access_token": f"at-{code}",
            "refresh_token": f"rt-{code}",
            "expires_at": int(time.time()) + 3600,
        },
    )
    monkeypatch.setattr(
        microsoft,
        "fetch_profile",
        lambda access_token: {
            "address": address,
            "display_name": name,
            "provider_account_id": "ms-user-1",
        },
    )
    set_provider("microsoft", MicrosoftProvider(client_factory=_BaselineOnlyClient))


def _connect(client, headers, monkeypatch, **profile) -> dict:
    _fake_exchange(monkeypatch, **profile)
    auth_url = client.post("/v1/integrations/microsoft/connect", headers=headers).json()[
        "auth_url"
    ]
    state = auth_url.split("state=")[1].split("&")[0]
    response = client.get(f"/v1/integrations/microsoft/callback?code=abc&state={state}")
    assert response.status_code == 200, response.text
    return client.get("/v1/integrations/mailboxes", headers=headers).json()


class TestTokenCrypto:
    def test_roundtrip(self):
        blob = encrypt_json({"access_token": "a", "refresh_token": "b", "expires_at": 1})
        assert "access_token" not in blob  # actually encrypted, not encoded
        assert decrypt_json(blob)["refresh_token"] == "b"

    def test_tampering_is_rejected(self):
        blob = encrypt_json({"access_token": "a"})
        with pytest.raises(ValueError):
            decrypt_json(blob[:-4] + "AAAA")


class TestProviderRegistry:
    def test_both_providers_are_listed(self, client, account):
        body = client.get("/v1/integrations/mailboxes", headers=account["headers"]).json()
        assert {entry["name"] for entry in body["providers"]} == {"microsoft", "zoho"}

    def test_an_unknown_provider_is_a_404_not_a_silent_default(self, client, account):
        """Falling back to Microsoft here would connect the wrong kind of
        mailbox and then send from it."""
        response = client.post(
            "/v1/integrations/gmail/connect", headers=account["headers"]
        )
        assert response.status_code == 404

    def test_state_minted_for_one_provider_is_rejected_by_another(
        self, client, account, ms_configured, monkeypatch
    ):
        _fake_exchange(monkeypatch)
        monkeypatch.setenv("ZOHO_CLIENT_ID", "z-id")
        monkeypatch.setenv("ZOHO_CLIENT_SECRET", "z-secret")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        auth_url = client.post(
            "/v1/integrations/microsoft/connect", headers=account["headers"]
        ).json()["auth_url"]
        state = auth_url.split("state=")[1].split("&")[0]
        response = client.get(f"/v1/integrations/zoho/callback?code=abc&state={state}")
        assert response.status_code == 400


class TestConnectFlow:
    def test_unconfigured_status_and_connect(self, client, account):
        headers = account["headers"]
        body = client.get("/v1/integrations/mailboxes", headers=headers).json()
        assert _provider_for("microsoft", body)["configured"] is False
        assert body["connections"] == []
        assert (
            client.post("/v1/integrations/microsoft/connect", headers=headers).status_code == 503
        )

    def test_connect_returns_a_tenant_auth_url(self, client, account, ms_configured):
        body = client.post(
            "/v1/integrations/microsoft/connect", headers=account["headers"]
        ).json()
        assert "login.microsoftonline.com/organizations" in body["auth_url"]
        assert "client_id=client-123" in body["auth_url"]
        assert "state=" in body["auth_url"]

    def test_callback_stores_an_encrypted_connection(
        self, client, account, session, ms_configured, monkeypatch
    ):
        status = _connect(client, account["headers"], monkeypatch)
        [connection] = status["connections"]
        assert connection["address"] == "tushar@goforay.io"
        assert connection["status"] == "active"
        assert connection["provider"] == "microsoft"
        assert "token_blob" not in connection  # never serialised out
        # A new mailbox joins the rotation with a conservative cap.
        assert connection["send_enabled"] is True
        assert connection["daily_send_cap"] == 40
        assert connection["sent_last_24h"] == 0

        row = session.get(MailboxConnection, connection["id"])
        assert "at-abc" not in row.token_blob
        assert decrypt_json(row.token_blob)["access_token"] == "at-abc"
        assert row.provider_account_id == "ms-user-1"
        # Cursors baseline at connect: a reply landing before the first worker
        # sync must not fall on the wrong side of the delta.
        assert row.inbox_delta_link == "https://graph.test/delta?token=connect"
        assert row.sent_delta_link == "https://graph.test/sent-delta?token=connect"

    def test_reconnect_updates_instead_of_duplicating(
        self, client, account, ms_configured, monkeypatch
    ):
        _connect(client, account["headers"], monkeypatch)
        status = _connect(client, account["headers"], monkeypatch, name="T. Gupta")
        assert len(status["connections"]) == 1
        assert status["connections"][0]["display_name"] == "T. Gupta"

    def test_forged_state_is_rejected(self, client, account, ms_configured, monkeypatch):
        _fake_exchange(monkeypatch)
        response = client.get("/v1/integrations/microsoft/callback?code=abc&state=forged")
        assert response.status_code == 400

    def test_login_token_is_not_a_valid_state(self, client, account, ms_configured, monkeypatch):
        """A bearer token must not double as connect state — wrong role."""
        _fake_exchange(monkeypatch)
        response = client.get(
            f"/v1/integrations/microsoft/callback?code=abc&state={account['token']}"
        )
        assert response.status_code == 400

    def test_user_denied_consent_shows_a_page_and_stores_nothing(
        self, client, account, ms_configured
    ):
        response = client.get("/v1/integrations/microsoft/callback?error=access_denied")
        assert response.status_code == 200
        body = client.get("/v1/integrations/mailboxes", headers=account["headers"]).json()
        assert body["connections"] == []

    def test_disconnect_revokes_and_clears_tokens(
        self, client, account, session, ms_configured, monkeypatch
    ):
        status = _connect(client, account["headers"], monkeypatch)
        connection_id = status["connections"][0]["id"]
        response = client.delete(
            f"/v1/integrations/mailboxes/{connection_id}", headers=account["headers"]
        )
        assert response.status_code == 200
        row = session.get(MailboxConnection, connection_id)
        assert row.status == "revoked" and row.token_blob == ""


class TestSendingControls:
    def test_cap_and_switch_are_editable(self, client, account, session):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        session.add(connection)
        session.commit()
        response = client.patch(
            f"/v1/integrations/mailboxes/{connection.id}/sending",
            json={"daily_send_cap": 12, "send_enabled": False},
            headers=account["headers"],
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["daily_send_cap"] == 12 and body["send_enabled"] is False

    def test_a_negative_cap_is_refused(self, client, account, session):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        session.add(connection)
        session.commit()
        assert (
            client.patch(
                f"/v1/integrations/mailboxes/{connection.id}/sending",
                json={"daily_send_cap": -1},
                headers=account["headers"],
            ).status_code
            == 422
        )


class TestTokenRefresh:
    def test_expired_token_refreshes_and_persists(self, session, account, monkeypatch):
        connection = MailboxConnection(
            org_id=account["org_id"],
            address="tushar@goforay.io",
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-1", "expires_at": 100}
            ),
        )
        session.add(connection)
        session.flush()
        monkeypatch.setattr(
            microsoft,
            "refresh_tokens",
            lambda rt: {
                "access_token": "new",
                "refresh_token": "rt-2",
                "expires_at": int(time.time()) + 3600,
            },
        )
        client = microsoft.GraphClient(session, connection)
        assert client._access_token() == "new"
        assert decrypt_json(connection.token_blob)["refresh_token"] == "rt-2"

    def test_failed_refresh_marks_the_connection(self, session, account, monkeypatch):
        connection = MailboxConnection(
            org_id=account["org_id"],
            address="tushar@goforay.io",
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-1", "expires_at": 100}
            ),
        )
        session.add(connection)
        session.flush()

        def boom(rt):
            raise microsoft.MicrosoftError("invalid_grant", status_code=400)

        monkeypatch.setattr(microsoft, "refresh_tokens", boom)
        client = microsoft.GraphClient(session, connection)
        with pytest.raises(microsoft.MicrosoftError):
            client._access_token()
        assert connection.status == "error"
        assert "invalid_grant" in connection.error


class FakeGraphClient:
    def __init__(self, *, reply_fails: bool = False, patch_fails: bool = False):
        self.drafts: list[dict] = []
        self.sent_ids: list[str] = []
        self.replied_to: list[str] = []
        self.patches: list[tuple[str, dict]] = []
        self.reply_fails = reply_fails
        self.patch_fails = patch_fails

    def create_draft(self, message):
        self.drafts.append(message)
        return {
            "id": f"graph-{len(self.drafts)}",
            "internetMessageId": f"<imid-{len(self.drafts)}@outlook.com>",
            "conversationId": "conv-1",
        }

    def create_reply_draft(self, message_id, comment_html):
        if self.reply_fails:
            raise microsoft.MicrosoftError("ErrorItemNotFound", status_code=404)
        self.replied_to.append(message_id)
        return {
            "id": "graph-reply-1",
            "internetMessageId": "<imid-reply-1@outlook.com>",
            # Graph puts the reply on the parent's conversation, which is the
            # whole point: same thread in the candidate's client.
            "conversationId": "conv-parent",
            "body": {"contentType": "HTML", "content": "<p>quoted original</p>"},
        }

    def update_draft(self, message_id, patch):
        if self.patch_fails:
            raise microsoft.MicrosoftError("ErrorInvalidPropertySet", status_code=400)
        self.patches.append((message_id, patch))
        return {"id": message_id, "conversationId": "conv-parent"}

    def send_draft(self, message_id):
        self.sent_ids.append(message_id)


class TestGraphSender:
    def test_send_builds_a_reply_matchable_message(self, session, account):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient()
        sender = microsoft.GraphSender(session, connection, client=fake)

        from juicebox.outreach.sender import OutboundEmail

        result = sender.send(
            OutboundEmail(
                to="ada@example.com",
                subject="Quick note",
                text_body="Hi Ada",
                html_body="<p>Hi Ada</p>",
                from_email="ignored@goforay.io",
                headers={"X-Juicebox-Message-Id": "msg_1", "List-Id": "must-not-pass"},
            )
        )
        assert result.ok
        assert result.provider_native_id == "graph-1"
        assert result.internet_message_id == "<imid-1@outlook.com>"
        assert result.conversation_id == "conv-1"
        assert fake.sent_ids == ["graph-1"]

        [draft] = fake.drafts
        assert draft["toRecipients"] == [{"emailAddress": {"address": "ada@example.com"}}]
        assert draft["body"]["contentType"] == "HTML"
        assert draft["internetMessageHeaders"] == [
            {"name": "X-Juicebox-Message-Id", "value": "msg_1"}
        ]


class TestGraphThreading:
    """A follow-up has to land in the thread the first email started.

    Graph takes ``internetMessageHeaders`` for x- names only, so In-Reply-To and
    References cannot get a send onto an Outlook thread. Before this, every
    follow-up arrived as a new conversation with the same subject -- verified in
    production, where step 2 went out under a fresh conversationId.
    """

    def _email(self, **kwargs):
        from juicebox.outreach.sender import OutboundEmail

        defaults = dict(
            to="ada@example.com",
            subject="Re: Quick note",
            text_body="Following up",
            html_body="<p>Following up</p>",
            from_email="ignored@goforay.io",
            headers={"X-Juicebox-Message-Id": "msg_2"},
            in_reply_to="<imid-1@outlook.com>",
            references="<imid-1@outlook.com>",
        )
        defaults.update(kwargs)
        return OutboundEmail(**defaults)

    def test_a_follow_up_replies_on_the_parents_thread(self, session, account):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient()
        sender = microsoft.GraphSender(session, connection, client=fake)

        result = sender.send(self._email(thread_native_id="graph-parent"))

        assert result.ok
        assert fake.replied_to == ["graph-parent"]
        # No fresh draft: a new draft is a new conversation.
        assert fake.drafts == []
        assert fake.sent_ids == ["graph-reply-1"]
        assert result.conversation_id == "conv-parent"

    def test_the_reply_goes_to_the_candidate_not_back_to_us(self, session, account):
        """createReply addresses the reply to the parent's From, and on a
        message we sent that is our own mailbox."""
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient()
        sender = microsoft.GraphSender(session, connection, client=fake)

        sender.send(self._email(thread_native_id="graph-parent"))

        [(draft_id, patch)] = fake.patches
        assert draft_id == "graph-reply-1"
        assert patch["toRecipients"] == [{"emailAddress": {"address": "ada@example.com"}}]
        assert patch["subject"] == "Re: Quick note"
        # Our body replaces Graph's quoted original: the quote carries the
        # previous step's tracking pixel.
        assert patch["body"]["content"] == "<p>Following up</p>"
        assert "quoted original" not in patch["body"]["content"]
        assert patch["internetMessageHeaders"] == [
            {"name": "X-Juicebox-Message-Id", "value": "msg_2"}
        ]

    def test_a_missing_parent_still_sends(self, session, account):
        """Deleted parent: unthreaded beats not sending at all."""
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient(reply_fails=True)
        sender = microsoft.GraphSender(session, connection, client=fake)

        result = sender.send(self._email(thread_native_id="graph-parent"))

        assert result.ok
        assert len(fake.drafts) == 1
        assert fake.sent_ids == ["graph-1"]

    def test_a_refused_reply_draft_still_sends(self, session, account):
        """The fallback covers the PATCH too: five failed attempts would park
        the send job and lose the step."""
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient(patch_fails=True)
        sender = microsoft.GraphSender(session, connection, client=fake)

        result = sender.send(self._email(thread_native_id="graph-parent"))

        assert result.ok
        assert fake.sent_ids == ["graph-1"]

    def test_a_first_send_is_a_plain_draft(self, session, account):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        fake = FakeGraphClient()
        sender = microsoft.GraphSender(session, connection, client=fake)

        sender.send(self._email(thread_native_id="", in_reply_to="", references=""))

        assert fake.replied_to == []
        assert len(fake.drafts) == 1


class TestSenderResolution:
    def test_sequence_without_mailbox_uses_the_global_provider(
        self, session, outreach_fixture, mailbox
    ):
        """No mailbox connected at all: rotation falls through to the globally
        configured provider, which is how every org behaved before the pool."""
        assert resolve_sender(session, outreach_fixture["sequence"]).sender is mailbox

    def test_missing_or_foreign_mailbox_fails_loudly(self, session, account):
        from juicebox.models import Sequence

        # Transient on purpose: the row never persists a dangling FK.
        sequence = Sequence(
            org_id=account["org_id"], name="x", sender_mode="fixed", mailbox_id="mbx_missing"
        )
        resolved = resolve_sender(session, sequence)
        assert isinstance(resolved.sender, FailingSender)
        assert not resolved.sender.send(None).ok

    def test_revoked_mailbox_fails_with_a_reconnect_hint(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(
            org_id=account["org_id"], address="t@goforay.io", status="revoked"
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id
        result = resolve_sender(session, sequence).sender.send(None)
        assert not result.ok and "reconnect" in result.error

    def test_a_pinned_enrollment_outranks_the_sequence(
        self, session, account, outreach_fixture
    ):
        """Once a conversation has gone out from an address it stays there, or
        the thread breaks in the candidate's client."""
        from juicebox.models import Enrollment

        pinned = MailboxConnection(org_id=account["org_id"], address="pinned@goforay.io")
        other = MailboxConnection(org_id=account["org_id"], address="other@goforay.io")
        session.add_all([pinned, other])
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = other.id
        set_sender_factory("microsoft", lambda db, conn: _Recorder(conn))

        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=outreach_fixture["candidates"][0].id,
            mailbox_id=pinned.id,
        )
        resolved = resolve_sender(session, sequence, enrollment)
        assert resolved.connection.id == pinned.id

    def test_run_due_sends_through_the_sequence_mailbox(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.sender_mode = "fixed"
        sequence.mailbox_id = connection.id

        captured: list = []

        class CapturingGraphSender:
            name = "graph"

            def __init__(self, db, conn):
                self.conn = conn

            def send(self, email):
                captured.append(email)
                return SendResult(
                    ok=True,
                    provider_message_id="<imid-1@outlook.com>",
                    provider_native_id="graph-1",
                    internet_message_id="<imid-1@outlook.com>",
                    conversation_id="conv-1",
                )

        set_sender_factory("microsoft", CapturingGraphSender)

        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        summary = run_due(session, org_id=sequence.org_id)
        session.commit()

        assert summary.sent == 1
        assert captured[0].to == "ada@example.com"
        message = session.query(Message).filter_by(direction="outbound").one()
        assert message.mailbox_id == connection.id
        assert message.provider_native_id == "graph-1"
        assert message.internet_message_id == "<imid-1@outlook.com>"
        assert message.conversation_id == "conv-1"
        assert message.provider_message_id == "<imid-1@outlook.com>"
        # The enrollment is now pinned to the mailbox that actually sent.
        from juicebox.models import Enrollment

        assert session.query(Enrollment).one().mailbox_id == connection.id


class _Recorder:
    name = "graph"

    def __init__(self, connection):
        self.connection = connection

    def send(self, email):
        return SendResult(ok=True, provider_native_id="rec-1")


class TestSequenceApiMailbox:
    def test_create_sequence_with_a_connected_mailbox(
        self, client, session, account, ms_configured, monkeypatch
    ):
        status = _connect(client, account["headers"], monkeypatch)
        mailbox_id = status["connections"][0]["id"]
        body = client.post(
            "/v1/sequences",
            json={
                "name": "Founding eng",
                "steps": [{"body_template": "Hi {{ first_name }}"}],
                "mailbox_id": mailbox_id,
            },
            headers=account["headers"],
        )
        assert body.status_code == 201, body.text
        assert body.json()["mailbox_id"] == mailbox_id
        # Naming a mailbox means pinning to it; otherwise the pool would ignore
        # the choice the caller just made.
        assert body.json()["sender_mode"] == "fixed"

    def test_a_sequence_defaults_to_the_rotation_pool(self, client, account):
        body = client.post(
            "/v1/sequences",
            json={"name": "Founding eng", "steps": [{"body_template": "Hi"}]},
            headers=account["headers"],
        )
        assert body.status_code == 201, body.text
        assert body.json()["sender_mode"] == "rotate"
        assert body.json()["mailbox_id"] is None

    def test_patch_sets_and_clears_the_mailbox(
        self, client, account, ms_configured, monkeypatch
    ):
        status = _connect(client, account["headers"], monkeypatch)
        mailbox_id = status["connections"][0]["id"]
        sequence = client.post(
            "/v1/sequences",
            json={"name": "Founding eng", "steps": [{"body_template": "Hi"}]},
            headers=account["headers"],
        ).json()

        patched = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_id": mailbox_id},
            headers=account["headers"],
        )
        assert patched.status_code == 200 and patched.json()["mailbox_id"] == mailbox_id
        assert patched.json()["sender_mode"] == "fixed"

        cleared = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_id": None},
            headers=account["headers"],
        )
        assert cleared.status_code == 200 and cleared.json()["mailbox_id"] is None
        assert cleared.json()["sender_mode"] == "rotate"

        assert (
            client.patch(
                f"/v1/sequences/{sequence['id']}",
                json={"mailbox_id": "mbx_nope"},
                headers=account["headers"],
            ).status_code
            == 404
        )

    def test_fixed_without_a_mailbox_is_refused(self, client, account):
        sequence = client.post(
            "/v1/sequences",
            json={"name": "Founding eng", "steps": [{"body_template": "Hi"}]},
            headers=account["headers"],
        ).json()
        assert (
            client.patch(
                f"/v1/sequences/{sequence['id']}",
                json={"sender_mode": "fixed"},
                headers=account["headers"],
            ).status_code
            == 422
        )

    def test_foreign_mailbox_id_is_rejected(self, client, account):
        response = client.post(
            "/v1/sequences",
            json={
                "name": "Founding eng",
                "steps": [{"body_template": "Hi"}],
                "mailbox_id": "mbx_of_someone_else",
            },
            headers=account["headers"],
        )
        assert response.status_code == 404

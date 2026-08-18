"""Microsoft Graph integration: OAuth connect flow, token storage, Graph sends."""

import time

import pytest

from juicebox.integrations import microsoft
from juicebox.integrations.crypto import decrypt_json, encrypt_json
from juicebox.models import MailboxConnection, Message
from juicebox.outreach.sender import (
    FailingSender,
    SendResult,
    resolve_sender,
    set_graph_sender_factory,
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
            "ms_user_id": "ms-user-1",
        },
    )
    monkeypatch.setattr(microsoft, "GraphClient", _BaselineOnlyClient)


def _connect(client, headers, monkeypatch, **profile) -> dict:
    _fake_exchange(monkeypatch, **profile)
    auth_url = client.post("/v1/integrations/microsoft/connect", headers=headers).json()[
        "auth_url"
    ]
    state = auth_url.split("state=")[1].split("&")[0]
    response = client.get(f"/v1/integrations/microsoft/callback?code=abc&state={state}")
    assert response.status_code == 200, response.text
    return client.get("/v1/integrations/microsoft", headers=headers).json()


class TestTokenCrypto:
    def test_roundtrip(self):
        blob = encrypt_json({"access_token": "a", "refresh_token": "b", "expires_at": 1})
        assert "access_token" not in blob  # actually encrypted, not encoded
        assert decrypt_json(blob)["refresh_token"] == "b"

    def test_tampering_is_rejected(self):
        blob = encrypt_json({"access_token": "a"})
        with pytest.raises(ValueError):
            decrypt_json(blob[:-4] + "AAAA")


class TestConnectFlow:
    def test_unconfigured_status_and_connect(self, client, account):
        headers = account["headers"]
        body = client.get("/v1/integrations/microsoft", headers=headers).json()
        assert body["configured"] is False and body["connections"] == []
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
        assert "token_blob" not in connection  # never serialised out

        row = session.get(MailboxConnection, connection["id"])
        assert "at-abc" not in row.token_blob
        assert decrypt_json(row.token_blob)["access_token"] == "at-abc"
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
        body = client.get("/v1/integrations/microsoft", headers=account["headers"]).json()
        assert body["connections"] == []

    def test_disconnect_revokes_and_clears_tokens(
        self, client, account, session, ms_configured, monkeypatch
    ):
        status = _connect(client, account["headers"], monkeypatch)
        connection_id = status["connections"][0]["id"]
        response = client.delete(
            f"/v1/integrations/microsoft/{connection_id}", headers=account["headers"]
        )
        assert response.status_code == 200
        row = session.get(MailboxConnection, connection_id)
        assert row.status == "revoked" and row.token_blob == ""


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
    def __init__(self):
        self.drafts: list[dict] = []
        self.sent_ids: list[str] = []

    def create_draft(self, message):
        self.drafts.append(message)
        return {
            "id": f"graph-{len(self.drafts)}",
            "internetMessageId": f"<imid-{len(self.drafts)}@outlook.com>",
            "conversationId": "conv-1",
        }

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
        assert result.internet_message_id == "<imid-1@outlook.com>"
        assert result.conversation_id == "conv-1"
        assert fake.sent_ids == ["graph-1"]

        [draft] = fake.drafts
        assert draft["toRecipients"] == [{"emailAddress": {"address": "ada@example.com"}}]
        assert draft["body"]["contentType"] == "HTML"
        assert draft["internetMessageHeaders"] == [
            {"name": "X-Juicebox-Message-Id", "value": "msg_1"}
        ]


class TestSenderResolution:
    def test_sequence_without_mailbox_uses_the_global_provider(
        self, session, outreach_fixture, mailbox
    ):
        assert resolve_sender(session, outreach_fixture["sequence"]) is mailbox

    def test_missing_or_foreign_mailbox_fails_loudly(self, session, account):
        from juicebox.models import Sequence

        # Transient on purpose: the row never persists a dangling FK.
        sequence = Sequence(org_id=account["org_id"], name="x", mailbox_id="mbx_missing")
        sender = resolve_sender(session, sequence)
        assert isinstance(sender, FailingSender)
        assert not sender.send(None).ok

    def test_revoked_mailbox_fails_with_a_reconnect_hint(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(
            org_id=account["org_id"], address="t@goforay.io", status="revoked"
        )
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
        sequence.mailbox_id = connection.id
        result = resolve_sender(session, sequence).send(None)
        assert not result.ok and "reconnect" in result.error

    def test_run_due_sends_through_the_sequence_mailbox(
        self, session, account, outreach_fixture
    ):
        connection = MailboxConnection(org_id=account["org_id"], address="t@goforay.io")
        session.add(connection)
        session.flush()
        sequence = outreach_fixture["sequence"]
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
                    graph_message_id="graph-1",
                    internet_message_id="<imid-1@outlook.com>",
                    conversation_id="conv-1",
                )

        set_graph_sender_factory(CapturingGraphSender)

        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        summary = run_due(session, org_id=sequence.org_id)
        session.commit()

        assert summary.sent == 1
        assert captured[0].to == "ada@example.com"
        message = session.query(Message).filter_by(direction="outbound").one()
        assert message.mailbox_id == connection.id
        assert message.internet_message_id == "<imid-1@outlook.com>"
        assert message.conversation_id == "conv-1"
        assert message.provider_message_id == "<imid-1@outlook.com>"


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

        cleared = client.patch(
            f"/v1/sequences/{sequence['id']}",
            json={"mailbox_id": None},
            headers=account["headers"],
        )
        assert cleared.status_code == 200 and cleared.json()["mailbox_id"] is None

        assert (
            client.patch(
                f"/v1/sequences/{sequence['id']}",
                json={"mailbox_id": "mbx_nope"},
                headers=account["headers"],
            ).status_code
            == 404
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

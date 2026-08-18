"""Microsoft Graph send provider and mailbox OAuth — all on FakeGraphTransport."""

from __future__ import annotations

import pytest

from juicebox.config import reset_settings_cache
from juicebox.models import MailboxConnection, Org, utcnow
from juicebox.outreach.graph import (
    FakeGraphTransport,
    GraphResponse,
    GraphSender,
    bind_mailbox_session,
    send_mail_payload,
    set_graph_transport,
    upsert_connection,
)
from juicebox.outreach.sender import OutboundEmail, get_sender, set_sender
from juicebox.outreach.sequencer import enroll_candidates, send_step
from juicebox.security import (
    TokenError,
    create_signed_state,
    decode_signed_state,
    decrypt_secret,
    encrypt_secret,
)


def _graph_env(monkeypatch, **extra: str) -> None:
    monkeypatch.setenv("GRAPH_CLIENT_ID", "test-client")
    monkeypatch.setenv("GRAPH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("GRAPH_TENANT_ID", "common")
    for key, value in extra.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()


def _email(**overrides) -> OutboundEmail:
    base = dict(
        to="ada@example.com",
        subject="Hello",
        text_body="Hi Ada",
        html_body="<p>Hi Ada</p>",
        from_email="talent@acme-talent.com",
        from_name="Acme",
        headers={"X-Juicebox-Message-Id": "msg_test", "List-Unsubscribe": "<http://x/t/u/1>"},
        org_id="org_test",
    )
    base.update(overrides)
    return OutboundEmail(**base)


def _connect(session, org_id: str, user_id: str | None = None, **profile) -> MailboxConnection:
    return upsert_connection(
        session,
        org_id=org_id,
        user_id=user_id,
        token_payload={
            "access_token": "fake-access",
            "refresh_token": "fake-refresh",
            "expires_in": 3600,
            "scope": "offline_access User.Read Mail.Send",
        },
        profile={
            "mail": "ada@contoso.com",
            "displayName": "Ada Recruiter",
            "userPrincipalName": "ada@contoso.com",
            **profile,
        },
    )


class TestSecretBox:
    def test_round_trip(self):
        blob = encrypt_secret("refresh-token-value")
        assert blob
        assert "refresh-token-value" not in blob
        assert decrypt_secret(blob) == "refresh-token-value"

    def test_tamper_is_rejected(self):
        blob = encrypt_secret("secret")
        with pytest.raises(TokenError):
            decrypt_secret(blob[:-2] + "ab")

    def test_empty_is_empty(self):
        assert encrypt_secret("") == ""
        assert decrypt_secret("") == ""

    def test_oauth_state_rejects_access_tokens(self, account):
        from juicebox.security import create_access_token

        token = create_access_token(
            subject=account["user_id"], org_id=account["org_id"], role="owner"
        )
        with pytest.raises(TokenError, match="wrong token type"):
            decode_signed_state(token)

        state = create_signed_state({"purpose": "microsoft_oauth", "org": "org_1"})
        assert decode_signed_state(state)["purpose"] == "microsoft_oauth"


class TestSendMailPayload:
    def test_maps_html_and_drops_non_x_headers(self):
        payload = send_mail_payload(_email())
        message = payload["message"]
        assert payload["saveToSentItems"] is True
        assert message["body"]["contentType"] == "HTML"
        assert message["toRecipients"][0]["emailAddress"]["address"] == "ada@example.com"
        names = [h["name"] for h in message["internetMessageHeaders"]]
        assert names == ["X-Juicebox-Message-Id"]
        assert message["internetMessageId"] == "<msg_test@juicebox.send>"


class TestGraphSender:
    def test_get_sender_selects_graph(self, monkeypatch):
        _graph_env(monkeypatch, EMAIL_PROVIDER="graph")
        set_sender(None)
        assert get_sender().name == "graph"

    def test_outlook_alias(self, monkeypatch):
        _graph_env(monkeypatch, EMAIL_PROVIDER="outlook")
        set_sender(None)
        assert get_sender().name == "graph"

    def test_send_uses_me_sendmail(self, session, account, monkeypatch):
        _graph_env(monkeypatch)
        transport = FakeGraphTransport.connected()
        _connect(session, account["org_id"], account["user_id"])
        session.commit()
        sender = GraphSender(transport=transport)
        with bind_mailbox_session(session):
            result = sender.send(_email(org_id=account["org_id"]))
        assert result.ok
        assert result.provider_message_id == "graph:msg_test"
        send_calls = [c for c in transport.calls if "sendMail" in c["url"]]
        assert send_calls
        assert send_calls[0]["url"].endswith("/me/sendMail")
        assert send_calls[0]["headers"]["Authorization"] == "Bearer fake-access"
        assert send_calls[0]["json"]["message"]["subject"] == "Hello"

    def test_missing_mailbox_fails(self, monkeypatch):
        _graph_env(monkeypatch)
        sender = GraphSender(transport=FakeGraphTransport.connected())
        result = sender.send(_email(org_id="org_missing"))
        assert not result.ok
        assert "mailbox" in result.error.lower()

    def test_graph_error_is_surfaced(self, session, account, monkeypatch):
        _graph_env(monkeypatch)
        transport = FakeGraphTransport.connected(
            **{"POST /sendMail": GraphResponse(400, {"error": {"message": "Mailbox over quota"}})}
        )
        _connect(session, account["org_id"])
        session.commit()
        sender = GraphSender(transport=transport)
        with bind_mailbox_session(session):
            result = sender.send(_email(org_id=account["org_id"]))
        assert not result.ok
        assert "quota" in result.error.lower()

    def test_401_refreshes_and_retries(self, session, account, monkeypatch):
        _graph_env(monkeypatch)
        sends = {"n": 0}

        def send_handler(_req):
            sends["n"] += 1
            if sends["n"] == 1:
                return GraphResponse(401, {"error": {"code": "InvalidAuthenticationToken"}})
            return GraphResponse(202, None)

        transport = FakeGraphTransport.connected(**{"POST /sendMail": send_handler})
        _connect(session, account["org_id"])
        session.commit()
        sender = GraphSender(transport=transport)
        with bind_mailbox_session(session):
            result = sender.send(_email(org_id=account["org_id"]))
        assert result.ok
        assert sends["n"] == 2
        assert any(
            "/token" in c["url"] and c["data"].get("grant_type") == "refresh_token"
            for c in transport.calls
        )

    def test_client_credentials_uses_users_path(self, monkeypatch):
        _graph_env(monkeypatch, GRAPH_MAILBOX="shared@contoso.com", GRAPH_TENANT_ID="tenant-1")
        transport = FakeGraphTransport.connected()
        sender = GraphSender(transport=transport)
        result = sender.send(_email(org_id="org_none"))
        assert result.ok
        send_calls = [c for c in transport.calls if "sendMail" in c["url"]]
        assert send_calls[0]["url"].endswith("/users/shared@contoso.com/sendMail")
        assert any(c["data"].get("grant_type") == "client_credentials" for c in transport.calls)


class TestSequencerGraph:
    def test_send_step_persists_graph_message_id(
        self, session, outreach_fixture, mailbox, monkeypatch
    ):
        _graph_env(monkeypatch)
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        db_org = session.get(Org, sequence.org_id)
        db_org.postal_address = "1 Market St, San Francisco, CA 94105"
        transport = FakeGraphTransport.connected()
        _connect(session, sequence.org_id)
        session.commit()
        set_sender(GraphSender(transport=transport))

        [enrollment] = enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        message = send_step(
            session,
            enrollment=enrollment,
            sequence=sequence,
            step=step,
            candidate=ada,
            now=utcnow(),
        )
        assert message.status == "sent"
        assert message.provider_message_id.startswith("graph:")
        assert any("sendMail" in c["url"] for c in transport.calls)

    def test_graph_refuses_without_postal(self, session, outreach_fixture, monkeypatch):
        _graph_env(monkeypatch)
        sequence = outreach_fixture["sequence"]
        ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
        transport = FakeGraphTransport.connected()
        _connect(session, sequence.org_id)
        session.commit()
        set_sender(GraphSender(transport=transport))
        [enrollment] = enroll_candidates(session, sequence=sequence, candidate_ids=[ada.id])
        session.commit()
        step = sorted(sequence.steps, key=lambda s: s.step_order)[0]
        message = send_step(
            session,
            enrollment=enrollment,
            sequence=sequence,
            step=step,
            candidate=ada,
            now=utcnow(),
        )
        assert message.status == "failed"
        assert "postal address" in message.error
        assert not any("sendMail" in c["url"] for c in transport.calls)


class TestMailboxApi:
    def test_authorize_requires_graph_config(self, client, account):
        response = client.get("/v1/mailboxes/microsoft/authorize", headers=account["headers"])
        assert response.status_code == 400
        assert "GRAPH_CLIENT_ID" in response.json()["detail"]

    def test_oauth_round_trip(self, client, account, session, monkeypatch):
        _graph_env(monkeypatch)
        transport = FakeGraphTransport.connected()
        set_graph_transport(transport)

        started = client.get("/v1/mailboxes/microsoft/authorize", headers=account["headers"])
        assert started.status_code == 200
        body = started.json()
        assert "login.microsoftonline.com" in body["url"]
        assert "Mail.Send" in body["url"]

        callback = client.get(
            "/v1/mailboxes/microsoft/callback",
            params={"code": "auth-code", "state": body["state"]},
            follow_redirects=False,
        )
        assert callback.status_code == 302
        assert "mailbox=connected" in callback.headers["location"]

        listed = client.get("/v1/mailboxes", headers=account["headers"]).json()
        assert listed["configured"] is True
        assert listed["connections"][0]["mailbox_email"] == "ada@contoso.com"
        assert "access_token" not in listed["connections"][0]

        stored = session.query(MailboxConnection).one()
        assert decrypt_secret(stored.access_token_enc) == "fake-access"
        assert decrypt_secret(stored.refresh_token_enc) == "fake-refresh"

        token_calls = [c for c in transport.calls if "/token" in c["url"]]
        assert token_calls[0]["data"]["code"] == "auth-code"
        assert token_calls[0]["data"]["grant_type"] == "authorization_code"

    def test_callback_rejects_bad_state(self, client, monkeypatch):
        _graph_env(monkeypatch)
        set_graph_transport(FakeGraphTransport.connected())
        response = client.get(
            "/v1/mailboxes/microsoft/callback",
            params={"code": "x", "state": "not-a-token"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "invalid_state" in response.headers["location"]

    def test_disconnect_is_org_scoped(self, client, account, monkeypatch):
        _graph_env(monkeypatch)
        set_graph_transport(FakeGraphTransport.connected())
        started = client.get("/v1/mailboxes/microsoft/authorize", headers=account["headers"])
        client.get(
            "/v1/mailboxes/microsoft/callback",
            params={"code": "auth-code", "state": started.json()["state"]},
            follow_redirects=False,
        )
        mailbox_id = client.get("/v1/mailboxes", headers=account["headers"]).json()["connections"][
            0
        ]["id"]

        rival = client.post(
            "/v1/auth/signup",
            json={
                "org_name": "Rival",
                "email": "rival@rival-talent.com",
                "password": "supersecret1",
            },
        ).json()
        rival_headers = {"Authorization": f"Bearer {rival['access_token']}"}
        assert (
            client.delete(f"/v1/mailboxes/{mailbox_id}", headers=rival_headers).status_code == 404
        )

        removed = client.delete(f"/v1/mailboxes/{mailbox_id}", headers=account["headers"])
        assert removed.status_code == 200
        assert client.get("/v1/mailboxes", headers=account["headers"]).json()["connections"] == []

    def test_api_key_cannot_start_oauth(self, client, account, monkeypatch):
        _graph_env(monkeypatch)
        created = client.post("/v1/auth/api-keys", headers=account["headers"]).json()
        response = client.get(
            "/v1/mailboxes/microsoft/authorize",
            headers={"X-API-Key": created["key"]},
        )
        assert response.status_code == 400

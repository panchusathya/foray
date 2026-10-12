"""Seeds connected with Google / Yahoo sign-in instead of an app password.

Written from failure items 22-31 in juicebox/deliverability/README.md before
the code. Google seeds are read through the Gmail API (labels say where a
message landed); Yahoo seeds through IMAP with an XOAUTH2 token.
"""

from __future__ import annotations

import imaplib
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

from juicebox.deliverability import seed_oauth
from juicebox.deliverability.seed_oauth import GmailApiReader
from juicebox.deliverability.seeds import ImapReader, Probe, SeedReadError
from juicebox.integrations.crypto import decrypt_json, encrypt_json
from juicebox.models import MailboxConnection, SeedInbox

SENT = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)


def _ms(when: datetime) -> str:
    return str(int(when.timestamp() * 1000))


def _seed(**overrides) -> SeedInbox:
    tokens = {
        "access_token": "good-token",
        "refresh_token": "refresh-1",
        "expires_at": int(time.time()) + 3600,
    }
    values = {
        "org_id": "org_x",
        "address": "seed@gmail.com",
        "kind": "gmail_api",
        "provider_label": "gmail",
        "secret_blob": encrypt_json(overrides.pop("tokens", tokens)),
    }
    values.update(overrides)
    return SeedInbox(**values)


class FakeGmail:
    """messages.list / messages.get over an in-memory mailbox, GET only."""

    def __init__(self, messages, *, expire_after=None):
        self.messages = messages
        self.calls: list[tuple[str, str, dict]] = []
        self.tokens_seen: list[str] = []
        self.expire_after = expire_after

    def __call__(self, method, url, params, token):
        assert method == "GET", "the seed reader must only ever read"
        self.calls.append((method, url, dict(params or {})))
        self.tokens_seen.append(token)
        if (
            self.expire_after is not None
            and len(self.calls) > self.expire_after
            and token == "good-token"
        ):
            return 401, {"error": {"code": 401}}
        if url.endswith("/messages"):
            q = params.get("q", "")
            hits = [m for m in self.messages if self._matches(m, q, params)]
            return 200, {"messages": [{"id": m["id"]} for m in hits]} if hits else {}
        message_id = url.rsplit("/", 1)[-1]
        m = next(m for m in self.messages if m["id"] == message_id)
        return 200, {
            "id": m["id"],
            "labelIds": m["labels"],
            "internalDate": _ms(m["received"]),
            "payload": {
                "headers": [
                    {"name": "From", "value": f"Ryan <{m['from']}>"},
                    {"name": "Subject", "value": m["subject"]},
                    {"name": "Message-ID", "value": m["message_id"]},
                ]
            },
        }

    def _matches(self, m, q, params):
        if "SPAM" in m["labels"] and params.get("includeSpamTrash") not in (True, "true"):
            return False
        if q.startswith("rfc822msgid:"):
            return m["message_id"].strip("<>") == q.split(":", 1)[1].strip("<>")
        if "from:" in q:
            sender = q.split("from:", 1)[1].split()[0]
            after = int(q.split("after:", 1)[1].split()[0])
            return m["from"] == sender and m["received"].timestamp() >= after
        raise AssertionError(f"unexpected query {q!r}")


def _msg(
    id="m1",
    labels=("INBOX", "CATEGORY_PERSONAL"),
    *,
    subject="Senior Backend Engineer at Acme",
    message_id="<abc@zoho.com>",
    received=SENT + timedelta(minutes=1),
):
    return {
        "id": id,
        "labels": list(labels),
        "from": "ryan@goforay.io",
        "subject": subject,
        "message_id": message_id,
        "received": received,
    }


def _probe(**overrides):
    values = {
        "from_address": "ryan@goforay.io",
        "subject": "Senior Backend Engineer at Acme",
        "message_id": "<abc@zoho.com>",
        "not_before": SENT,
    }
    values.update(overrides)
    return Probe(**values)


def _gmail_reader(seed, fake, refresh=None):
    return GmailApiReader(
        seed,
        transport=fake,
        refresh=refresh or (lambda provider, token: pytest.fail("refresh not expected")),
    )


class TestGmailPlacement:
    def test_primary_inbox(self):
        fake = FakeGmail([_msg()])
        with _gmail_reader(_seed(), fake) as reader:
            result = reader.find(_probe())
        assert (result.found, result.placement, result.folder) == (True, "inbox", "INBOX")

    def test_promotions_tab(self):
        fake = FakeGmail([_msg(labels=("INBOX", "CATEGORY_PROMOTIONS"))])
        with _gmail_reader(_seed(), fake) as reader:
            result = reader.find(_probe())
        assert (result.placement, result.folder) == ("other_tab", "promotions")

    def test_spam_is_searched_on_purpose(self):
        fake = FakeGmail([_msg(labels=("SPAM",))])
        with _gmail_reader(_seed(), fake) as reader:
            assert reader.find(_probe()).placement == "spam"
        assert all(
            c[2].get("includeSpamTrash") in (True, "true")
            for c in fake.calls
            if c[1].endswith("/messages")
        )

    def test_sender_and_subject_fallback(self):
        fake = FakeGmail([_msg(message_id="<rewritten@gmail>")])
        with _gmail_reader(_seed(), fake) as reader:
            assert reader.find(_probe()).placement == "inbox"

    def test_fallback_ignores_other_subjects_and_older_copies(self):
        fake = FakeGmail(
            [
                _msg("a", subject="Quick question", message_id="<q@x>"),
                _msg("b", message_id="<old@x>", received=SENT - timedelta(days=1)),
            ]
        )
        with _gmail_reader(_seed(), fake) as reader:
            assert not reader.find(_probe(message_id="")).found


class TestGmailTokens:
    def test_an_expired_token_is_refreshed_and_kept(self):
        seed = _seed(
            tokens={"access_token": "stale", "refresh_token": "refresh-1", "expires_at": 0}
        )
        fake = FakeGmail([_msg()])
        calls = []

        def refresh(provider, token):
            calls.append((provider, token))
            return {
                "access_token": "fresh",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
            }

        with _gmail_reader(seed, fake, refresh) as reader:
            reader.find(_probe())
        assert calls == [("google", "refresh-1")]
        assert set(fake.tokens_seen) == {"fresh"}
        stored = decrypt_json(seed.secret_blob)
        assert stored["access_token"] == "fresh"
        assert stored["refresh_token"] == "refresh-1"  # carried, not blanked

    def test_a_401_mid_read_refreshes_once_and_retries(self):
        fake = FakeGmail([_msg()], expire_after=0)
        refreshed = []

        def refresh(provider, token):
            refreshed.append(token)
            return {
                "access_token": "fresh",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
            }

        with _gmail_reader(_seed(), fake, refresh) as reader:
            assert reader.find(_probe()).placement == "inbox"
        assert refreshed == ["refresh-1"]

    def test_a_revoked_sign_in_is_a_read_error_not_a_miss(self):
        seed = _seed(tokens={"access_token": "stale", "refresh_token": "dead", "expires_at": 0})

        def refresh(provider, token):
            raise seed_oauth.SeedOAuthError("invalid_grant")

        with pytest.raises(SeedReadError) as caught, _gmail_reader(seed, FakeGmail([]), refresh):
            pass
        assert "reconnect" in str(caught.value).lower()
        assert "dead" not in str(caught.value) and "stale" not in str(caught.value)


class XoauthServer:
    """Enough IMAP for the reader, recording how it authenticated."""

    capabilities = ("IMAP4REV1", "AUTH=XOAUTH2")

    def __init__(self):
        self.sasl = None
        self.logged_in_with_password = False

    def login(self, user, password):
        self.logged_in_with_password = True
        raise imaplib.IMAP4.error(b"LOGIN with a token")

    def authenticate(self, mechanism, authobject):
        assert mechanism == "XOAUTH2"
        self.sasl = authobject(b"")
        return "OK", [b"ok"]

    def list(self, *args):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "Bulk"']

    def select(self, mailbox="INBOX", readonly=False):
        assert readonly
        return "OK", [b"0"]

    def search(self, charset, *criteria):
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class TestYahooXoauth:
    def test_the_token_goes_through_authenticate_not_login(self):
        server = XoauthServer()
        reader = ImapReader(
            host="imap.mail.yahoo.com",
            port=993,
            username="seed@yahoo.com",
            access_token="yahoo-token",
            factory=lambda h, p, timeout=None: server,
        )
        with reader:
            reader.find(_probe())
        assert not server.logged_in_with_password
        sasl = server.sasl.decode() if isinstance(server.sasl, bytes) else server.sasl
        assert sasl == "user=seed@yahoo.com\x01auth=Bearer yahoo-token\x01\x01"

    def test_a_yahoo_seed_reader_refreshes_an_expired_token_first(self, monkeypatch):
        seed = _seed(
            address="seed@yahoo.com",
            kind="imap_oauth",
            provider_label="yahoo",
            imap_host="imap.mail.yahoo.com",
            imap_port=993,
            tokens={"access_token": "stale", "refresh_token": "y-refresh", "expires_at": 0},
        )
        monkeypatch.setattr(
            seed_oauth,
            "_refresh",
            lambda provider, token: {
                "access_token": "fresh-y",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
            },
        )
        reader = seed_oauth.reader_for_oauth_seed(seed)
        assert isinstance(reader, ImapReader)
        assert reader.access_token == "fresh-y"
        assert decrypt_json(seed.secret_blob)["refresh_token"] == "y-refresh"


# -- connecting -----------------------------------------------------------------


@pytest.fixture
def providers_configured(monkeypatch):
    from juicebox.config import reset_settings_cache

    for key, value in {
        "GMAIL_CLIENT_ID": "gclient",
        "GMAIL_CLIENT_SECRET": "gsecret",
        "YAHOO_CLIENT_ID": "yclient",
        "YAHOO_CLIENT_SECRET": "ysecret",
    }.items():
        monkeypatch.setenv(key, value)
    reset_settings_cache()
    yield
    reset_settings_cache()


def _connect(client, account, provider):
    response = client.post(
        f"/v1/deliverability/seeds/oauth/{provider}/connect", headers=account["headers"]
    )
    assert response.status_code == 200, response.text
    url = response.json()["auth_url"]
    return url, parse_qs(urlparse(url).query)


@pytest.fixture
def exchange(monkeypatch):
    holder = {
        "result": (
            "seed@gmail.com",
            {
                "access_token": "a1",
                "refresh_token": "r1",
                "expires_at": int(time.time()) + 3600,
                "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
            },
        )
    }

    def fake(provider, code):
        holder["provider"] = provider
        return holder["result"]

    monkeypatch.setattr(seed_oauth, "_exchange", fake)
    return holder


class TestConnect:
    def test_unconfigured_provider_says_so(self, client, account):
        response = client.post(
            "/v1/deliverability/seeds/oauth/yahoo/connect", headers=account["headers"]
        )
        assert response.status_code == 503
        assert "YAHOO_CLIENT_ID" in response.json()["detail"]

    def test_unknown_provider(self, client, account, providers_configured):
        response = client.post(
            "/v1/deliverability/seeds/oauth/hotmail/connect", headers=account["headers"]
        )
        assert response.status_code == 404

    def test_google_asks_to_read_and_nothing_else(self, client, account, providers_configured):
        _url, query = _connect(client, account, "google")
        scopes = query["scope"][0].split()
        assert "https://www.googleapis.com/auth/gmail.readonly" in scopes
        assert not any(s.endswith(("gmail.send", "gmail.compose", "gmail.modify")) for s in scopes)
        # The callback Google already knows about: no new redirect to register.
        assert query["redirect_uri"][0].endswith("/v1/integrations/gmail/callback")

    def test_yahoo_asks_for_mail_read(self, client, account, providers_configured):
        _url, query = _connect(client, account, "yahoo")
        assert "mail-r" in query["scope"][0].split()
        assert query["redirect_uri"][0].endswith("/v1/deliverability/seeds/yahoo/callback")


class TestCallback:
    def test_a_google_seed_connect_creates_a_seed_and_no_mailbox(
        self, client, account, providers_configured, exchange, session
    ):
        _url, query = _connect(client, account, "google")
        response = client.get(f"/v1/integrations/gmail/callback?code=c&state={query['state'][0]}")
        assert response.status_code == 200
        assert "seed" in response.text.lower()
        seed = session.query(SeedInbox).one()
        assert (seed.address, seed.kind, seed.provider_label) == (
            "seed@gmail.com",
            "gmail_api",
            "gmail",
        )
        assert decrypt_json(seed.secret_blob)["refresh_token"] == "r1"
        assert session.query(MailboxConnection).count() == 0

    def test_a_yahoo_seed_connect(self, client, account, providers_configured, exchange, session):
        exchange["result"] = (
            "seed@yahoo.com",
            {
                "access_token": "y1",
                "refresh_token": "yr",
                "expires_at": int(time.time()) + 3600,
                "scope": "openid mail-r",
            },
        )
        _url, query = _connect(client, account, "yahoo")
        response = client.get(
            f"/v1/deliverability/seeds/yahoo/callback?code=c&state={query['state'][0]}"
        )
        assert response.status_code == 200
        seed = session.query(SeedInbox).one()
        assert (seed.kind, seed.imap_host, seed.provider_label) == (
            "imap_oauth",
            "imap.mail.yahoo.com",
            "yahoo",
        )
        assert exchange["provider"] == "yahoo"

    def test_unticking_the_read_permission_is_refused(
        self, client, account, providers_configured, exchange, session
    ):
        exchange["result"] = (
            "seed@gmail.com",
            {"access_token": "a", "refresh_token": "r", "expires_at": 0, "scope": "openid email"},
        )
        _url, query = _connect(client, account, "google")
        response = client.get(f"/v1/integrations/gmail/callback?code=c&state={query['state'][0]}")
        assert "read" in response.text.lower()
        assert session.query(SeedInbox).count() == 0

    def test_an_active_sending_mailbox_cannot_be_a_seed_but_a_revoked_one_can(
        self, client, account, providers_configured, exchange, session
    ):
        session.add(
            MailboxConnection(org_id=account["org_id"], address="seed@gmail.com", provider="gmail")
        )
        session.commit()
        _url, query = _connect(client, account, "google")
        client.get(f"/v1/integrations/gmail/callback?code=c&state={query['state'][0]}")
        assert session.query(SeedInbox).count() == 0

        session.query(MailboxConnection).update({"status": "revoked"})
        session.commit()
        _url, query = _connect(client, account, "google")
        client.get(f"/v1/integrations/gmail/callback?code=c&state={query['state'][0]}")
        assert session.query(SeedInbox).count() == 1

    def test_reconnecting_refreshes_the_seed_and_keeps_its_refresh_token(
        self, client, account, providers_configured, exchange, session
    ):
        session.add(
            SeedInbox(
                org_id=account["org_id"],
                address="seed@gmail.com",
                kind="gmail_api",
                provider_label="gmail",
                status="error",
                error="Google refused the stored sign-in",
                secret_blob=encrypt_json({"access_token": "old", "refresh_token": "keep-me"}),
            )
        )
        session.commit()
        exchange["result"] = (
            "seed@gmail.com",
            {
                "access_token": "new",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
                "scope": "https://www.googleapis.com/auth/gmail.readonly",
            },
        )
        _url, query = _connect(client, account, "google")
        client.get(f"/v1/integrations/gmail/callback?code=c&state={query['state'][0]}")
        session.expire_all()
        seed = session.query(SeedInbox).one()
        assert (seed.status, seed.error) == ("active", "")
        stored = decrypt_json(seed.secret_blob)
        assert {"access_token": "new", "refresh_token": "keep-me"}.items() <= stored.items()

    def test_a_mailbox_state_is_refused_at_the_yahoo_seed_callback(
        self, client, account, providers_configured, exchange, session
    ):
        mailbox_state = client.post(
            "/v1/integrations/gmail/connect", headers=account["headers"]
        ).json()["auth_url"]
        state = parse_qs(urlparse(mailbox_state).query)["state"][0]
        response = client.get(f"/v1/deliverability/seeds/yahoo/callback?code=c&state={state}")
        assert response.status_code == 400
        assert session.query(SeedInbox).count() == 0

    def test_a_google_seed_state_is_refused_at_the_yahoo_callback(
        self, client, account, providers_configured, exchange, session
    ):
        _url, query = _connect(client, account, "google")
        response = client.get(
            f"/v1/deliverability/seeds/yahoo/callback?code=c&state={query['state'][0]}"
        )
        assert response.status_code == 400

    def test_an_ordinary_gmail_mailbox_connect_still_makes_a_mailbox(
        self, client, account, providers_configured, session, monkeypatch
    ):
        from juicebox.integrations.gmail import GmailProvider
        from juicebox.integrations.providers import ConnectResult

        monkeypatch.setattr(
            GmailProvider,
            "exchange_code",
            lambda self, code, params: ConnectResult(
                tokens={"access_token": "x", "refresh_token": "y"}, address="sender@gmail.com"
            ),
        )
        monkeypatch.setattr(
            "juicebox.routers.integrations._baseline_cursors", lambda db, connection, provider: None
        )
        url = client.post("/v1/integrations/gmail/connect", headers=account["headers"]).json()[
            "auth_url"
        ]
        state = parse_qs(urlparse(url).query)["state"][0]
        client.get(f"/v1/integrations/gmail/callback?code=c&state={state}")
        assert session.query(MailboxConnection).count() == 1
        assert session.query(SeedInbox).count() == 0


class TestPasswordSeedsAndRevokedMailboxes:
    def test_a_revoked_mailbox_address_can_be_added_as_a_password_seed(
        self, client, account, session, monkeypatch
    ):
        from juicebox.deliverability import seeds as seeds_module

        class Ok:
            spam_visible = True

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(seeds_module, "_reader_factory", lambda seed: Ok())
        session.add(
            MailboxConnection(
                org_id=account["org_id"],
                address="old@gmail.com",
                provider="gmail",
                status="revoked",
            )
        )
        session.commit()
        response = client.post(
            "/v1/deliverability/seeds",
            json={"address": "old@gmail.com", "provider_label": "gmail", "password": "abcd"},
            headers=account["headers"],
        )
        assert response.status_code == 201, response.text

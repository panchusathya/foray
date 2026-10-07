"""Zoho Mail provider: data-centre routing, sends, timestamp polling, drafts.

The three things worth defending here are the ones that fail silently rather
than loudly: aiming the token exchange at the wrong data centre, dropping the
ids that reply matching depends on, and advancing a high-water mark past mail
that was never read.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from juicebox.integrations import zoho
from juicebox.integrations.crypto import decrypt_json, encrypt_json
from juicebox.integrations.providers import FOLDER_INBOX, FOLDER_SENT
from juicebox.integrations.zoho import ZohoClient, ZohoError, ZohoProvider, ZohoSender
from juicebox.models import MailboxConnection
from juicebox.outreach.sender import OutboundEmail


@pytest.fixture
def zoho_configured(monkeypatch):
    monkeypatch.setenv("ZOHO_CLIENT_ID", "zclient")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "zsecret")
    monkeypatch.setenv("ZOHO_ACCOUNTS_DOMAIN", "https://accounts.zoho.com")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


def _connection(org_id, **kwargs) -> MailboxConnection:
    defaults = {
        "provider": "zoho",
        "address": "tushar@goforay.io",
        "provider_account_id": "999",
        "api_base": "https://mail.zoho.eu",
        "auth_base": "https://accounts.zoho.eu",
        "token_blob": encrypt_json(
            {"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}
        ),
        "sync_state": {"folders": {FOLDER_INBOX: "10", FOLDER_SENT: "20", "drafts": "30"}},
    }
    defaults.update(kwargs)
    return MailboxConnection(org_id=org_id, **defaults)


class FakeZohoClient:
    """Stands in for ZohoClient at the HTTP boundary."""

    def __init__(self, *, pages=None, send_response=None, content="Full body text"):
        # pages: list of message dicts, newest first, as messages/view returns.
        self.rows = list(pages or [])
        self.send_response = send_response
        self.content = content
        self.sent: list[dict] = []
        self.list_calls: list[tuple[str, int, int]] = []
        self.content_calls: list[str] = []
        self.header_calls: list[str] = []
        self.headers: dict[str, dict] = {}

    def list_folder(self, folder_id, *, start, limit):
        self.list_calls.append((folder_id, start, limit))
        return self.rows[start - 1 : start - 1 + limit]

    def fetch_content(self, folder_id, message_id):
        self.content_calls.append(message_id)
        return {"content": self.content}

    def fetch_header(self, folder_id, message_id):
        self.header_calls.append(message_id)
        return self.headers.get(message_id, {})

    def send_message(self, body):
        self.sent.append(body)
        if self.send_response is None:
            return {"messageId": "170001", "mailId": "<abc@goforay.io>"}
        return self.send_response


def raw_headers(*, in_reply_to="", message_id="<child@goforay.io>") -> dict:
    """A Zoho /header response, shaped the way the live API shapes it.

    One string under ``headerContent`` holding the entire RFC header block --
    ARC seals and all, which is why the real thing runs to a couple of KB.
    """
    crlf = chr(13) + chr(10)
    tab = chr(9)
    lines = [
        "Delivered-To: tushar@goforay.io",
        "ARC-Seal: i=1; a=rsa-sha256; t=1787205330; cv=none;",
        tab + "d=zohomail.com; s=zohoarc;",
        "Message-ID: " + message_id,
        "Subject: Re: role",
    ]
    if in_reply_to:
        lines += ["In-Reply-To: " + in_reply_to, "References: " + in_reply_to]
    return {"headerContent": crlf.join(lines) + crlf, "messageId": "170001"}


def _row(
    *,
    message_id="170001",
    received_ms=1_700_000_000_000,
    sender="ada@example.com",
    subject="Re: role",
    thread_id="thread-1",
    mode="",
):
    return {
        "messageId": message_id,
        "receivedTime": str(received_ms),
        "fromAddress": sender,
        "toAddress": "tushar@goforay.io",
        "subject": subject,
        "summary": "truncated preview",
        "threadId": thread_id,
        "hasAttachment": "false",
        "mode": mode,
    }


class TestDataCentreRouting:
    def test_accounts_host_maps_to_the_mail_host(self):
        assert zoho.api_base_for("https://accounts.zoho.eu") == "https://mail.zoho.eu"
        assert zoho.api_base_for("accounts.zoho.com.au") == "https://mail.zoho.com.au"
        # Already a mail host: left alone rather than mangled.
        assert zoho.api_base_for("https://mail.zoho.in") == "https://mail.zoho.in"

    def test_the_callback_decides_the_data_centre(self, zoho_configured):
        """Zoho reports the mailbox's region on the redirect, and the token
        exchange must follow it -- not the domain we sent the user to."""
        assert (
            zoho.resolve_auth_base({"accounts-server": "https://accounts.zoho.eu"})
            == "https://accounts.zoho.eu"
        )
        # Bare host, as Zoho sometimes sends it.
        assert zoho.resolve_auth_base({"accounts-server": "accounts.zoho.in"}) == (
            "https://accounts.zoho.in"
        )
        # Nothing reported: fall back to the configured domain.
        assert zoho.resolve_auth_base({}) == "https://accounts.zoho.com"

    def test_exchange_targets_the_reported_host(self, zoho_configured, monkeypatch):
        seen: list[str] = []

        def fake_token_request(auth_base, grant):
            seen.append(auth_base)
            return {"access_token": "at", "refresh_token": "rt", "expires_at": 1}

        monkeypatch.setattr(zoho, "_token_request", fake_token_request)
        monkeypatch.setattr(
            ZohoProvider,
            "_fetch_account",
            lambda self, token, api_base: {
                "id": "999",
                "address": "tushar@goforay.io",
                "display_name": "Tushar",
            },
        )
        monkeypatch.setattr(
            ZohoProvider,
            "_fetch_folder_ids",
            lambda self, token, api_base, account_id: {
                FOLDER_INBOX: "10",
                FOLDER_SENT: "20",
            },
        )
        result = ZohoProvider().exchange_code(
            "code-1", {"accounts-server": "https://accounts.zoho.eu", "location": "eu"}
        )
        assert seen == ["https://accounts.zoho.eu"]
        assert result.auth_base == "https://accounts.zoho.eu"
        assert result.api_base == "https://mail.zoho.eu"
        assert result.provider_account_id == "999"
        # Folder ids are resolved once at connect; every later call needs them.
        assert result.sync_state["folders"][FOLDER_INBOX] == "10"

    def test_a_200_with_an_error_body_is_still_a_failure(self, zoho_configured, monkeypatch):
        """Zoho answers 200 with {"error": ...} for a bad code or redirect URI,
        so the status code alone is not the success signal."""

        class Response:
            status_code = 200
            text = '{"error": "invalid_code"}'

            def json(self):
                return {"error": "invalid_code"}

        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, *args, **kwargs):
                return Response()

        monkeypatch.setattr(zoho.httpx, "Client", lambda **kwargs: Client())
        with pytest.raises(ZohoError, match="invalid_code"):
            zoho.exchange_code("bad", "https://accounts.zoho.com")

    def test_authorize_url_asks_for_a_refresh_token(self, zoho_configured):
        url = zoho.authorize_url("state-1")
        assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "ZohoMail.messages.ALL" in url.replace("%2C", ",").replace("+", " ")


class TestTokenRefresh:
    def test_a_refresh_without_a_new_refresh_token_keeps_the_old_one(
        self, session, account, zoho_configured, monkeypatch
    ):
        connection = _connection(
            account["org_id"],
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-1", "expires_at": 100}
            ),
        )
        session.add(connection)
        session.flush()
        monkeypatch.setattr(
            zoho,
            "refresh_tokens",
            lambda rt, auth_base: {
                "access_token": "new",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
            },
        )
        assert ZohoClient(session, connection)._access_token() == "new"
        stored = decrypt_json(connection.token_blob)
        assert stored["refresh_token"] == "rt-1"

    def test_the_refresh_goes_back_to_the_stored_auth_base(
        self, session, account, zoho_configured, monkeypatch
    ):
        connection = _connection(
            account["org_id"],
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-1", "expires_at": 100}
            ),
        )
        session.add(connection)
        session.flush()
        seen: list[str] = []
        monkeypatch.setattr(
            zoho,
            "refresh_tokens",
            lambda rt, auth_base: seen.append(auth_base)
            or {"access_token": "n", "refresh_token": "", "expires_at": 2**40},
        )
        ZohoClient(session, connection)._access_token()
        assert seen == ["https://accounts.zoho.eu"]

    def test_a_failed_refresh_marks_the_connection(
        self, session, account, zoho_configured, monkeypatch
    ):
        connection = _connection(
            account["org_id"],
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-1", "expires_at": 100}
            ),
        )
        session.add(connection)
        session.flush()

        def boom(rt, auth_base):
            raise ZohoError("invalid_grant")

        monkeypatch.setattr(zoho, "refresh_tokens", boom)
        with pytest.raises(ZohoError):
            ZohoClient(session, connection)._access_token()
        assert connection.status == "error" and "invalid_grant" in connection.error


class TestSender:
    def test_send_maps_both_ids_off_the_response(self, session, account):
        """messageId is what ingest dedupes on; mailId is what a later reply's
        In-Reply-To will be matched against. Losing either breaks detection."""
        connection = _connection(account["org_id"])
        fake = FakeZohoClient()
        result = ZohoSender(session, connection, client=fake).send(
            OutboundEmail(
                to="ada@example.com",
                subject="Quick note",
                text_body="Hi Ada",
                html_body="<p>Hi Ada</p>",
                from_email="spoofed@elsewhere.com",
            )
        )
        assert result.ok
        assert result.provider_native_id == "170001"
        assert result.internet_message_id == "<abc@goforay.io>"

        [body] = fake.sent
        # From is forced to the connected mailbox, matching Graph, which cannot
        # spoof at all.
        assert body["fromAddress"] == "tushar@goforay.io"
        assert body["toAddress"] == "ada@example.com"
        assert body["mailFormat"] == "html"

    def test_the_send_records_a_thread_id_replies_can_be_matched_on(
        self, session, account
    ):
        """Zoho's send response has no threadId, but a reply to this message
        arrives with threadId == this message's own id. Leaving the column
        empty is what made every Zoho reply fall through to address matching.
        """
        connection = _connection(account["org_id"])
        result = ZohoSender(session, connection, client=FakeZohoClient()).send(
            OutboundEmail(
                to="ada@example.com",
                subject="s",
                text_body="b",
                html_body="<p>b</p>",
                from_email=connection.address,
            )
        )
        assert result.provider_native_id == "170001"
        assert result.conversation_id == "170001"

    def test_threading_headers_are_passed_when_present(self, session, account):
        connection = _connection(account["org_id"])
        fake = FakeZohoClient()
        ZohoSender(session, connection, client=fake).send(
            OutboundEmail(
                to="ada@example.com",
                subject="Re: role",
                text_body="Following up",
                html_body="<p>Following up</p>",
                from_email="tushar@goforay.io",
                in_reply_to="<parent@goforay.io>",
                references="<parent@goforay.io>",
            )
        )
        [body] = fake.sent
        assert body["inReplyTo"] == "<parent@goforay.io>"
        assert body["refHeader"] == "<parent@goforay.io>"

    def test_a_response_without_a_message_id_is_not_a_success(self, session, account):
        """Reporting ok here would leave a row nothing can dedupe or attach a
        reply draft to -- a send that silently breaks reply detection."""
        connection = _connection(account["org_id"])
        fake = FakeZohoClient(send_response={"mailId": "<abc@goforay.io>"})
        result = ZohoSender(session, connection, client=fake).send(
            OutboundEmail(
                to="ada@example.com",
                subject="s",
                text_body="b",
                html_body="<p>b</p>",
                from_email="tushar@goforay.io",
            )
        )
        assert not result.ok and "messageId" in result.error


class TestEventParsing:
    def test_zoho_field_shapes_become_a_mailbox_event(self):
        event = zoho.to_event(
            {
                "messageId": "170001",
                "receivedTime": "1700000000000",
                "fromAddress": "Ada Okonkwo <ada@example.com>",
                "toAddress": "tushar@goforay.io,other@goforay.io",
                "ccAddress": "cc@goforay.io",
                "subject": "Re: role",
                "summary": "preview",
                "threadId": "thread-9",
                "mailId": "<xyz@example.com>",
                "hasAttachment": "true",
            },
            body="<p>the whole body</p>",
        )
        assert event.native_id == "170001"
        assert event.from_address == "ada@example.com"
        assert event.to_addresses == (
            "tushar@goforay.io",
            "other@goforay.io",
            "cc@goforay.io",
        )
        assert event.thread_id == "thread-9"
        assert event.rfc_message_id == "<xyz@example.com>"
        assert event.body == "<p>the whole body</p>" and event.body_is_html
        assert event.has_attachments
        assert event.received_at is not None
        assert event.received_at.year == 2023

    def test_a_sent_time_ahead_of_filing_is_clamped_to_it(self):
        """Zoho's sentDateInGMT on mail composed in Zoho runs ahead by the
        account's UTC offset; prod saw +7h on every Pacific account. Trusted,
        one manual reply pinned the domain's gap clock seven hours out."""
        filed = 1_791_309_356_456  # receivedTime, a true epoch
        row = {**_row(received_ms=filed), "sentDateInGMT": str(filed + 7 * 3600 * 1000)}

        event = zoho.to_event(row)

        assert event.sent_at == event.received_at
        assert event.occurred_at.timestamp() == filed / 1000

    def test_an_honest_sent_time_is_kept(self):
        """Mail from outside is stamped by its sender before Zoho files it."""
        filed = 1_791_309_356_456
        row = {**_row(received_ms=filed), "sentDateInGMT": str(filed - 2000)}

        assert zoho.to_event(row).sent_at.timestamp() == (filed - 2000) / 1000

    def test_a_draft_is_marked_as_one(self):
        """A draft ingested as a reply would stop a live sequence."""
        assert zoho.to_event(_row(mode="draft")).is_draft
        assert not zoho.to_event(_row()).is_draft


class TestFolderPolling:
    def test_the_first_pass_only_baselines(self, session, account):
        connection = _connection(account["org_id"], sync_state={"folders": {FOLDER_INBOX: "10"}})
        session.add(connection)
        session.flush()
        fake = FakeZohoClient(pages=[_row(message_id="a", received_ms=1_700_000_000_000)])
        provider = ZohoProvider(client_factory=lambda db, conn: fake)

        page = provider.iter_folder(session, connection, FOLDER_INBOX, cursor="")
        assert page.initialized and page.events == []
        # Only the newest row is read, and no body is fetched: old mail is not
        # outreach traffic.
        assert fake.list_calls == [("10", 1, 1)]
        assert fake.content_calls == []
        assert json.loads(page.next_cursor)["mark"] == 1_700_000_000_000

    def test_a_later_pass_reads_only_what_is_newer(self, session, account):
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        rows = [
            _row(message_id="new-2", received_ms=1_700_000_002_000),
            _row(message_id="new-1", received_ms=1_700_000_001_000),
            _row(message_id="old", received_ms=1_699_999_999_000),
        ]
        fake = FakeZohoClient(pages=rows)
        provider = ZohoProvider(client_factory=lambda db, conn: fake)
        cursor = json.dumps({"mark": 1_700_000_000_000, "seen": []})

        page = provider.iter_folder(session, connection, FOLDER_INBOX, cursor=cursor)
        assert [event.native_id for event in page.events] == ["new-2", "new-1"]
        assert json.loads(page.next_cursor)["mark"] == 1_700_000_002_000
        # The body is fetched for genuinely new mail only, because intent
        # classification on a truncated summary misreads long replies.
        assert sorted(fake.content_calls) == ["new-1", "new-2"]

    def test_committing_the_cursor_survives_a_reload(self, session, account):
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        provider = ZohoProvider(client_factory=lambda db, conn: FakeZohoClient())
        provider.commit_cursor(session, connection, FOLDER_INBOX, "cursor-1")
        session.flush()
        session.expire(connection)
        assert provider.read_cursor(connection, FOLDER_INBOX) == "cursor-1"
        # Folder ids are not clobbered by a cursor write.
        assert provider._folder_id(connection, FOLDER_INBOX) == "10"

    def test_a_second_pass_over_the_same_mail_reads_nothing(self, session, account):
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        rows = [_row(message_id="new-1", received_ms=1_700_000_001_000)]
        fake = FakeZohoClient(pages=rows)
        provider = ZohoProvider(client_factory=lambda db, conn: fake)

        first = provider.iter_folder(
            session,
            connection,
            FOLDER_INBOX,
            cursor=json.dumps({"mark": 1_700_000_000_000, "seen": []}),
        )
        assert len(first.events) == 1
        second = provider.iter_folder(
            session, connection, FOLDER_INBOX, cursor=first.next_cursor
        )
        assert second.events == []

    def test_messages_sharing_a_millisecond_are_neither_dropped_nor_replayed(
        self, session, account
    ):
        """Zoho timestamps collide, so the cursor carries the ids seen at the
        mark: a strict > would drop the sibling, a >= would replay forever."""
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        rows = [
            _row(message_id="twin-b", received_ms=1_700_000_001_000),
            _row(message_id="twin-a", received_ms=1_700_000_001_000),
        ]
        provider = ZohoProvider(client_factory=lambda db, conn: FakeZohoClient(pages=rows))
        first = provider.iter_folder(
            session,
            connection,
            FOLDER_INBOX,
            cursor=json.dumps({"mark": 1_700_000_000_000, "seen": []}),
        )
        assert {event.native_id for event in first.events} == {"twin-a", "twin-b"}
        assert set(json.loads(first.next_cursor)["seen"]) == {"twin-a", "twin-b"}

        # A third message lands on the very same millisecond.
        rows.insert(0, _row(message_id="twin-c", received_ms=1_700_000_001_000))
        provider = ZohoProvider(client_factory=lambda db, conn: FakeZohoClient(pages=rows))
        second = provider.iter_folder(
            session, connection, FOLDER_INBOX, cursor=first.next_cursor
        )
        assert [event.native_id for event in second.events] == ["twin-c"]

    def test_paging_walks_back_with_a_one_based_start(
        self, session, account, monkeypatch
    ):
        """start is 1-based and limit caps at 200, so catching up is several
        pages -- one page of the newest N is not the same thing."""
        monkeypatch.setattr(zoho, "_PAGE_LIMIT", 2)
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        rows = [
            _row(message_id=f"n{i}", received_ms=1_700_000_010_000 - i * 1000)
            for i in range(5)
        ] + [_row(message_id="old", received_ms=1_699_999_000_000)]
        fake = FakeZohoClient(pages=rows)
        provider = ZohoProvider(client_factory=lambda db, conn: fake)

        page = provider.iter_folder(
            session,
            connection,
            FOLDER_INBOX,
            cursor=json.dumps({"mark": 1_700_000_000_000, "seen": []}),
        )
        assert [event.native_id for event in page.events] == ["n0", "n1", "n2", "n3", "n4"]
        assert [call[1] for call in fake.list_calls] == [1, 3, 5]

    def test_an_unreachable_mark_holds_the_cursor_back(
        self, session, account, monkeypatch
    ):
        """Advancing past a gap would skip mail permanently, so the mark stays
        put and the next pass carries on."""
        monkeypatch.setattr(zoho, "_PAGE_LIMIT", 2)
        monkeypatch.setattr(zoho, "_MAX_CATCHUP_PAGES", 1)
        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        rows = [
            _row(message_id=f"n{i}", received_ms=1_700_000_010_000 - i * 1000)
            for i in range(6)
        ]
        provider = ZohoProvider(client_factory=lambda db, conn: FakeZohoClient(pages=rows))
        page = provider.iter_folder(
            session,
            connection,
            FOLDER_INBOX,
            cursor=json.dumps({"mark": 1_700_000_000_000, "seen": []}),
        )
        assert len(page.events) == 2
        assert page.next_cursor == ""

    def test_headers_come_back_as_one_raw_rfc_block(self):
        """What Zoho actually returns: the whole header block as a single
        string, not a key/value map. Believing otherwise silently reduced Zoho
        reply matching to sender-address guesswork."""
        header = raw_headers(in_reply_to="<parent@goforay.io>")
        assert zoho._header_value(header, "in-reply-to") == "<parent@goforay.io>"
        assert zoho._header_value(header, "message-id") == "<child@goforay.io>"
        # Case-insensitive, and absent headers are empty rather than an error.
        assert zoho._header_value(header, "In-Reply-To") == "<parent@goforay.io>"
        assert zoho._header_value(header, "x-nonexistent") == ""
        assert zoho._header_value({}, "in-reply-to") == ""

    def test_a_folded_header_is_not_truncated(self):
        """References routinely folds across lines; a naive line split would
        return only the first id."""
        crlf = chr(13) + chr(10)
        tab = chr(9)
        blob = crlf.join(
            [
                "Message-ID: <child@goforay.io>",
                "References: <one@goforay.io>",
                tab + "<two@goforay.io>",
                " <three@goforay.io>",
                "Subject: Re: role",
            ]
        ) + crlf
        value = zoho._header_value({"headerContent": blob}, "references")
        assert value == "<one@goforay.io> <two@goforay.io> <three@goforay.io>"


class TestReplyDraft:
    def test_the_draft_is_saved_on_the_thread(self, session, account, outreach_fixture):
        from juicebox.models import Message

        connection = _connection(account["org_id"])
        session.add(connection)
        session.flush()
        inbound = Message(
            org_id=account["org_id"],
            candidate_id=outreach_fixture["candidates"][0].id,
            direction="inbound",
            status="received",
            from_address="ada@example.com",
            to_address="tushar@goforay.io",
            subject="Re: role",
            internet_message_id="<parent@example.com>",
            provider_native_id="170002",
        )
        session.add(inbound)
        session.flush()

        fake = FakeZohoClient(send_response={"messageId": "draft-77"})
        provider = ZohoProvider(client_factory=lambda db, conn: fake)
        placed = provider.place_reply_draft(
            session, connection=connection, inbound=inbound, html_body="<p>Sounds good</p>"
        )
        assert placed.provider_draft_id == "draft-77"

        [body] = fake.sent
        # Saved, not sent: the recruiter edits and sends it themselves.
        assert body["mode"] == "draft"
        assert body["toAddress"] == "ada@example.com"
        assert body["subject"] == "Re: role"
        assert body["inReplyTo"] == "<parent@example.com>"

    def test_quoting_escapes_a_plaintext_original(self, session, account):
        """A plain-text reply containing angle brackets must not be able to
        inject markup into the draft."""
        inbound = SimpleNamespace(
            body="<script>alert(1)</script> and <b>bold</b>",
            body_html="",
            from_address="ada@example.com",
            sent_at=None,
        )
        quoted = zoho._quote_original(inbound)
        assert "&lt;script&gt;" in quoted
        assert "<script>" not in quoted


class TestEscapedRecipients:
    """Zoho HTML-escapes the angle brackets in recipient fields.

    `toAddress` arrives as `&lt;anna+token@goforay.io&gt;`. Every parser looks
    for `<` and `>` to find the address inside a display-name value, so without
    unescaping the brackets are never found, `&lt;` stays glued to the local
    part and `&gt;` to the domain, and the address matches nothing.

    This made every message sent to an application alias unroutable in
    production while all of these tests passed against clean addresses — which
    is exactly why the escaped form is now in the fixtures.
    """

    def test_a_plain_escaped_recipient(self):
        from juicebox.integrations.zoho import _addresses

        raw = "&lt;anna+h7uwa0ejlpjiseb8d9@goforay.io&gt;"
        assert _addresses(raw) == ["anna+h7uwa0ejlpjiseb8d9@goforay.io"]

    def test_an_escaped_recipient_with_a_display_name(self):
        from juicebox.integrations.zoho import _addresses

        raw = "Anna &lt;anna+tok@goforay.io&gt;"
        assert _addresses(raw) == ["anna+tok@goforay.io"]

    def test_unescaped_input_still_works(self):
        """Zoho is not consistent across views, so both shapes must parse."""
        from juicebox.integrations.zoho import _addresses

        assert _addresses("<anna+tok@goforay.io>") == ["anna+tok@goforay.io"]
        assert _addresses("anna+tok@goforay.io") == ["anna+tok@goforay.io"]

    def test_the_display_name_parser_unescapes_too(self):
        from juicebox.integrations.zoho import _named_addresses

        raw = "Dana Whitfield &lt;dana@parasail.ai&gt;"
        assert _named_addresses(raw) == [("dana@parasail.ai", "Dana Whitfield")]

    def test_an_escaped_alias_survives_all_the_way_to_a_token(self, monkeypatch):
        """The end the bug actually broke: an alias that parses to nothing is
        mail nobody can attribute, and it fails silently."""
        from juicebox.applications import alias
        from juicebox.config import reset_settings_cache
        from juicebox.integrations.zoho import _addresses

        monkeypatch.setenv("APPLY_ALIAS_ADDRESS", "anna@goforay.io")
        reset_settings_cache()
        try:
            escaped = "&lt;anna+h7uwa0ejlpjiseb8d9@goforay.io&gt;"
            # What production actually did before the fix.
            assert alias.parse(escaped) == ""
            # And what it does now.
            assert alias.parse(_addresses(escaped)[0]) == "h7uwa0ejlpjiseb8d9"
        finally:
            reset_settings_cache()

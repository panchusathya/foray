"""Gmail provider, one test per entry in the failure list it was written from.

The fake stands in at the HTTP boundary (GmailClient's methods) and returns
payloads shaped the way the Gmail REST API shapes them: base64url bodies,
header lists, ``internalDate`` as a millisecond string, labels on every message.
"""

from __future__ import annotations

import base64
import email
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from juicebox.integrations import gmail
from juicebox.integrations.crypto import decrypt_json, encrypt_json
from juicebox.integrations.gmail import GmailError, GmailProvider, GmailSender
from juicebox.integrations.providers import FOLDER_INBOX, FOLDER_SENT, get_provider
from juicebox.models import MailboxConnection
from juicebox.outreach.sender import OutboundEmail


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def message(
    msg_id: str,
    *,
    labels=("INBOX",),
    sender="Ada Okonkwo <ada@example.com>",
    to="Sathya <sathya@goforay.io>",
    cc="",
    subject="Re: Founding AE role",
    ts_ms=1_791_400_000_000,
    thread="t1",
    html="<p>Yes, keen to chat</p>",
    plain="Yes, keen to chat",
    headers=None,
    attachment=False,
) -> dict:
    hdrs = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{msg_id}@mail.gmail.com>"},
        {"name": "Date", "value": "Fri, 01 Jan 2100 00:00:00 +0000"},  # a lying Date header
    ]
    if cc:
        hdrs.append({"name": "Cc", "value": cc})
    for name, value in (headers or {}).items():
        hdrs.append({"name": name, "value": value})
    parts = [
        {"mimeType": "text/plain", "body": {"data": b64(plain)}, "headers": []},
        {
            "mimeType": "text/html",
            "headers": [{"name": "Content-Type", "value": 'text/html; charset="UTF-8"'}],
            "body": {"data": b64(html)},
        },
    ]
    if attachment:
        parts.append(
            {"mimeType": "application/pdf", "filename": "cv.pdf", "body": {"attachmentId": "a1"}}
        )
    return {
        "id": msg_id,
        "threadId": thread,
        "labelIds": list(labels),
        "internalDate": str(ts_ms),
        "snippet": plain[:40],
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": hdrs,
            "parts": [{"mimeType": "multipart/alternative", "parts": parts}],
        },
    }


class FakeGmail:
    """GmailClient's methods over an in-memory mailbox."""

    def __init__(self):
        self.messages: dict[str, dict] = {}
        self.history_pages: list[dict] = []
        self.history_error: GmailError | None = None
        self.listings: list[dict] = []
        self.profile = {"emailAddress": "sathya@goforay.io", "historyId": "1000"}
        self.sent: list[dict] = []
        self.drafts: list[dict] = []
        self.send_response: dict | None = None
        self.calls: list[tuple] = []
        self.stored_message_id = ""
        self.fail_lookup = False

    def get_profile(self):
        return self.profile

    def list_history(self, start, label, page_token=""):
        self.calls.append(("history", start, label, page_token))
        if self.history_error:
            raise self.history_error
        index = int(page_token or 0)
        return self.history_pages[index]

    def list_messages(self, *, q="", label_ids=(), page_token="", max_results=100):
        self.calls.append(("list", q, tuple(label_ids), page_token))
        index = int(page_token or 0)
        return self.listings[index] if index < len(self.listings) else {}

    def get_message(self, message_id, *, fmt="full", headers=()):
        self.calls.append(("get", message_id, fmt, tuple(headers)))
        if fmt == "minimal" and self.fail_lookup:
            raise GmailError("boom", status_code=500)
        if message_id not in self.messages:
            raise GmailError("not found", status_code=404)
        found = self.messages[message_id]
        if fmt == "metadata" and self.stored_message_id:
            return {
                "id": message_id,
                "payload": {"headers": [{"name": "Message-ID", "value": self.stored_message_id}]},
            }
        if fmt == "metadata":
            wanted = {h.lower() for h in headers}
            payload = {
                "headers": [h for h in found["payload"]["headers"] if h["name"].lower() in wanted]
            }
            return {**{k: v for k, v in found.items() if k != "payload"}, "payload": payload}
        return found

    def send(self, raw, thread_id=""):
        self.sent.append({"raw": raw, "threadId": thread_id})
        if self.send_response is not None:
            return self.send_response
        new_id = f"s{len(self.sent)}"
        stored = message(new_id, labels=("SENT",), thread=thread_id or "tnew")
        # Real Gmail keeps the Message-ID the client set; store that one.
        ours = parse_raw(raw)["Message-ID"]
        for header in stored["payload"]["headers"]:
            if header["name"] == "Message-ID":
                header["value"] = ours
        self.messages[new_id] = stored
        return {"id": new_id, "threadId": thread_id or "tnew"}

    def create_draft(self, raw, thread_id=""):
        self.drafts.append({"raw": raw, "threadId": thread_id})
        return {"id": "r1", "message": {"id": "d1", "threadId": thread_id}}


def connection(org_id, **kwargs) -> MailboxConnection:
    defaults = {
        "provider": "gmail",
        "address": "sathya@goforay.io",
        "display_name": "Sathya Panchu",
        "token_blob": encrypt_json(
            {"access_token": "at", "refresh_token": "rt", "expires_at": 2**40}
        ),
        "sync_state": {"cursors": {}},
    }
    defaults.update(kwargs)
    return MailboxConnection(org_id=org_id, **defaults)


def provider_with(fake: FakeGmail) -> GmailProvider:
    return GmailProvider(client_factory=lambda db, conn: fake)


def parse_raw(raw: str):
    return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


def outbound(**kwargs) -> OutboundEmail:
    base = dict(
        to="ada@example.com",
        subject="Founding AE role",
        text_body="Hi Ada",
        html_body="<p>Hi Ada</p>",
        from_email="sathya@goforay.io",
    )
    base.update(kwargs)
    return OutboundEmail(**base)


@pytest.fixture
def gmail_configured(monkeypatch):
    monkeypatch.setenv("GMAIL_CLIENT_ID", "gclient")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "gsecret")
    from juicebox.config import reset_settings_cache

    reset_settings_cache()
    yield
    reset_settings_cache()


class TestRegistration:
    def test_gmail_is_a_registered_provider(self):
        assert get_provider("gmail").name == "gmail"

    def test_unconfigured_until_both_credentials_are_set(self, gmail_configured, monkeypatch):
        assert GmailProvider().is_configured()
        monkeypatch.setenv("GMAIL_CLIENT_SECRET", "")
        from juicebox.config import reset_settings_cache

        reset_settings_cache()
        assert not GmailProvider().is_configured()

    def test_authorize_url_asks_for_offline_access_and_every_scope(self, gmail_configured):
        url = GmailProvider().authorize_url("STATE")
        assert "access_type=offline" in url and "state=STATE" in url
        for scope in gmail.REQUIRED_SCOPES:
            assert scope.replace(":", "%3A").replace("/", "%2F") in url


class TestConsent:
    """Failure 1: a partial grant must not connect."""

    def test_a_partial_grant_is_refused(self, gmail_configured, monkeypatch):
        granted = f"openid email {gmail.SCOPE_READ} {gmail.SCOPE_COMPOSE}"  # send unticked
        monkeypatch.setattr(
            gmail,
            "exchange_code",
            lambda code: {
                "access_token": "a",
                "refresh_token": "r",
                "expires_at": 0,
                "scope": granted,
            },
        )
        with pytest.raises(GmailError, match=r"gmail.send"):
            GmailProvider().exchange_code("code", {})

    def test_missing_scopes_names_only_what_is_missing(self):
        assert gmail.missing_scopes(" ".join(gmail.REQUIRED_SCOPES)) == []
        assert gmail.missing_scopes(gmail.SCOPE_READ) == [gmail.SCOPE_SEND, gmail.SCOPE_COMPOSE]


class TestTokens:
    """Failure 2: a dead grant shows on the mailbox, and refreshes keep the old refresh token."""

    def test_a_failed_refresh_marks_the_mailbox(self, session, account, monkeypatch):
        conn = connection(
            account["org_id"],
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt", "expires_at": 0}
            ),
        )
        session.add(conn)
        session.flush()

        def refused(_):
            raise GmailError("token request failed: invalid_grant", status_code=400)

        monkeypatch.setattr(gmail, "refresh_tokens", refused)
        with pytest.raises(GmailError):
            gmail.GmailClient(session, conn)._access_token()
        assert conn.status == "error" and "reconnect" in conn.error

    def test_a_refresh_without_a_refresh_token_keeps_the_old_one(
        self, session, account, monkeypatch
    ):
        conn = connection(
            account["org_id"],
            token_blob=encrypt_json(
                {"access_token": "old", "refresh_token": "rt-keep", "expires_at": 0}
            ),
        )
        session.add(conn)
        session.flush()
        monkeypatch.setattr(
            gmail,
            "refresh_tokens",
            lambda _: {
                "access_token": "new",
                "refresh_token": "",
                "expires_at": int(time.time()) + 3600,
                "scope": "",
            },
        )
        assert gmail.GmailClient(session, conn)._access_token() == "new"
        assert decrypt_json(conn.token_blob)["refresh_token"] == "rt-keep"


class TestSending:
    def test_a_first_email_goes_out_as_the_mailbox_with_our_message_id(self, account):
        fake = FakeGmail()
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(
            outbound(from_email="someone@else.com")
        )
        assert result.ok and result.provider_native_id == "s1" and result.conversation_id == "tnew"
        sent = parse_raw(fake.sent[0]["raw"])
        assert (
            "sathya@goforay.io" in sent["From"] and "someone@else.com" not in sent["From"]
        )  # failure 11
        assert sent["Message-ID"] == result.internet_message_id  # read back
        assert fake.sent[0]["threadId"] == ""

    def test_a_follow_up_threads_on_gmails_thread_id_and_headers(self, account):
        """Failure 3: the parent's message id is looked up to its thread."""
        fake = FakeGmail()
        fake.messages["parent"] = message("parent", labels=("SENT",), thread="thread-9")
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(
            outbound(
                subject="Re: Founding AE role",
                in_reply_to="<p@x>",
                references="<p@x>",
                thread_native_id="parent",
            )
        )
        assert fake.sent[0]["threadId"] == "thread-9" and result.conversation_id == "thread-9"
        sent = parse_raw(fake.sent[0]["raw"])
        assert sent["In-Reply-To"] == "<p@x>" and sent["References"] == "<p@x>"

    def test_a_failed_thread_lookup_still_sends(self, account):
        fake = FakeGmail()
        fake.fail_lookup = True
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(
            outbound(thread_native_id="parent")
        )
        assert result.ok and fake.sent[0]["threadId"] == ""

    def test_a_send_with_no_id_is_a_failure(self, account):
        """Failure 4: no native id, no success."""
        fake = FakeGmail()
        fake.send_response = {"labelIds": ["SENT"]}
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(outbound())
        assert not result.ok and "no message id" in result.error

    def test_the_message_id_gmail_stored_wins(self, account):
        """Failure 5: if Gmail rewrote our Message-ID, reply matching must use its."""
        fake = FakeGmail()
        fake.stored_message_id = "<CAGmail-rewritten@mail.gmail.com>"
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(outbound())
        assert result.internet_message_id == "<CAGmail-rewritten@mail.gmail.com>"

    def test_a_line_break_in_the_subject_is_refused_not_sent(self, account):
        """Failure 11: no header injection."""
        fake = FakeGmail()
        result = GmailSender(None, connection(account["org_id"]), client=fake).send(
            outbound(subject="Hi\r\nBcc: victim@x.com")
        )
        assert not result.ok and not fake.sent

    def test_custom_headers_ride_along(self, account):
        fake = FakeGmail()
        GmailSender(None, connection(account["org_id"]), client=fake).send(
            outbound(headers={"X-Juicebox-Message-Id": "msg_1"})
        )
        assert parse_raw(fake.sent[0]["raw"])["X-Juicebox-Message-Id"] == "msg_1"


class TestReading:
    def test_the_first_pass_baselines_and_reads_nothing(self, account):
        fake = FakeGmail()
        page = provider_with(fake).iter_folder(
            None, connection(account["org_id"]), FOLDER_INBOX, cursor=""
        )
        assert page.initialized and not page.events
        assert gmail._cursor_state(page.next_cursor)[0] == "1000"

    def test_new_inbox_mail_becomes_events_with_gmails_own_time(self, account):
        """Failure 10: internalDate, not the (lying) Date header."""
        fake = FakeGmail()
        fake.messages["m1"] = message("m1", attachment=True)
        fake.history_pages = [
            {"historyId": "1010", "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}]}
        ]
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 0),
        )
        [event] = page.events
        assert event.native_id == "m1" and event.from_address == "ada@example.com"
        assert event.thread_id == "t1" and event.rfc_message_id == "<m1@mail.gmail.com>"
        assert (
            event.body == "<p>Yes, keen to chat</p>"
            and event.body_is_html
            and event.has_attachments
        )
        assert event.received_at == datetime.fromtimestamp(1_791_400_000, tz=UTC)
        assert gmail._cursor_state(page.next_cursor) == ("1010", 1_791_400_000_000)

    def test_history_pages_are_all_read_before_the_cursor_moves(self, account):
        """Failure 7."""
        fake = FakeGmail()
        for i in (1, 2):
            fake.messages[f"m{i}"] = message(f"m{i}")
        fake.history_pages = [
            {
                "historyId": "1020",
                "nextPageToken": "1",
                "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
            },
            {"historyId": "1020", "history": [{"messagesAdded": [{"message": {"id": "m2"}}]}]},
        ]
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 0),
        )
        assert [e.native_id for e in page.events] == ["m1", "m2"]
        assert gmail._cursor_state(page.next_cursor)[0] == "1020"

    def test_running_out_of_pages_holds_the_cursor_back(self, account, monkeypatch):
        monkeypatch.setattr(gmail, "_MAX_PAGES", 1)
        fake = FakeGmail()
        fake.messages["m1"] = message("m1")
        fake.history_pages = [
            {
                "historyId": "1020",
                "nextPageToken": "1",
                "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
            }
        ]
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 0),
        )
        assert page.events and page.next_cursor == ""  # empty = keep the old cursor

    def test_expired_history_catches_up_by_date_instead_of_skipping(self, account):
        """Failure 6."""
        fake = FakeGmail()
        fake.history_error = GmailError("history gone", status_code=404)
        fake.messages["late"] = message("late", ts_ms=1_791_500_000_000)
        fake.listings = [{"messages": [{"id": "late"}]}]
        fake.profile = {"historyId": "5000"}
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 1_791_400_000_000),
        )
        assert [e.native_id for e in page.events] == ["late"]
        list_call = next(c for c in fake.calls if c[0] == "list")
        assert list_call[1] == f"after:{1_791_400_000 - 60}" and list_call[2] == ("INBOX",)
        assert gmail._cursor_state(page.next_cursor) == ("5000", 1_791_500_000_000)

    def test_other_history_errors_are_not_swallowed(self, account):
        fake = FakeGmail()
        fake.history_error = GmailError("server", status_code=500)
        with pytest.raises(GmailError):
            provider_with(fake).iter_folder(
                None,
                connection(account["org_id"]),
                FOLDER_INBOX,
                cursor=gmail._encode_cursor("1000", 0),
            )

    def test_a_message_deleted_before_fetch_is_skipped(self, account):
        """Failure 9."""
        fake = FakeGmail()
        fake.messages["kept"] = message("kept")
        fake.history_pages = [
            {
                "historyId": "1002",
                "history": [
                    {"messagesAdded": [{"message": {"id": "gone"}}, {"message": {"id": "kept"}}]}
                ],
            }
        ]
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 0),
        )
        assert [e.native_id for e in page.events] == ["kept"]

    def test_a_draft_is_marked_as_one(self):
        """Failure 8: a draft read as a reply would stop a sequence."""
        assert gmail.to_event(message("d", labels=("DRAFT",))).is_draft
        assert not gmail.to_event(message("m")).is_draft

    def test_only_messages_still_carrying_the_folders_label_count(self, account):
        fake = FakeGmail()
        fake.messages["moved"] = message("moved", labels=("CATEGORY_UPDATES",))
        fake.history_pages = [
            {"historyId": "1001", "history": [{"messagesAdded": [{"message": {"id": "moved"}}]}]}
        ]
        page = provider_with(fake).iter_folder(
            None,
            connection(account["org_id"]),
            FOLDER_INBOX,
            cursor=gmail._encode_cursor("1000", 0),
        )
        assert page.events == []

    def test_a_quoted_comma_name_is_one_recipient(self):
        event = gmail.to_event(
            message("m", to='"Smith, John" <js@x.com>, ana@y.com', cc="Cc Person <cc@z.com>")
        )
        assert event.to_addresses == ("js@x.com", "ana@y.com", "cc@z.com")

    def test_cursors_are_reassigned_not_mutated(self, account):
        conn = connection(account["org_id"])
        before = conn.sync_state
        GmailProvider().commit_cursor(None, conn, FOLDER_SENT, '{"history": "9", "ts": 1}')
        assert (
            conn.sync_state is not before
            and GmailProvider().read_cursor(conn, FOLDER_SENT) == '{"history": "9", "ts": 1}'
        )


class TestDiscoveryAndDrafts:
    def test_the_sent_sweep_never_asks_for_a_body(self, account):
        """Failure 12."""
        fake = FakeGmail()
        fake.messages["s1"] = message(
            "s1", labels=("SENT",), to='"Lee, Ann" <ann@co.com>', cc="bob@co.com"
        )
        fake.listings = [{"messages": [{"id": "s1"}], "nextPageToken": "1"}]
        page = provider_with(fake).iter_sent_history(
            None, connection(account["org_id"]), since=datetime(2026, 1, 1, tzinfo=UTC)
        )
        [record] = page.records
        assert record.to_addresses == ("ann@co.com",) and record.cc_addresses == ("bob@co.com",)
        assert (
            record.names == {"ann@co.com": "Lee, Ann"} and page.has_more and page.next_cursor == "1"
        )
        gets = [c for c in fake.calls if c[0] == "get"]
        assert gets and all(c[2] == "metadata" and set(c[3]) == {"From", "To", "Cc"} for c in gets)

    def test_a_reply_draft_lands_on_the_thread_with_threading_headers(self, account):
        fake = FakeGmail()
        inbound = SimpleNamespace(
            from_address="ada@example.com",
            to_address="",
            subject="Founding AE role",
            internet_message_id="<in@x>",
            conversation_id="thread-7",
            body="Keen",
            body_html="",
            sent_at=None,
        )
        result = provider_with(fake).place_reply_draft(
            None,
            connection=connection(account["org_id"]),
            inbound=inbound,
            html_body="<p>Great</p>",
        )
        assert result.provider_draft_id == "r1" and fake.drafts[0]["threadId"] == "thread-7"
        draft = parse_raw(fake.drafts[0]["raw"])
        assert draft["Subject"] == "Re: Founding AE role" and draft["In-Reply-To"] == "<in@x>"
        assert draft["To"] == "ada@example.com"


class TestThroughTheSequencer:
    """The sequencer sends through a Gmail mailbox without knowing it is Gmail."""

    def test_a_sequence_step_sends_from_a_gmail_mailbox(
        self, session, outreach_fixture, monkeypatch
    ):
        from juicebox.integrations.providers import set_provider
        from juicebox.models import Enrollment, Message
        from juicebox.outreach.sequencer import enroll_candidates, run_due

        fake = FakeGmail()
        set_provider("gmail", provider_with(fake))
        monkeypatch.setattr(GmailProvider, "is_configured", lambda self: True)
        try:
            seq = outreach_fixture["sequence"]
            conn = connection(seq.org_id)
            session.add(conn)
            session.flush()
            seq.sender_mode, seq.mailbox_id = "fixed", conn.id
            ada = next(c for c in outreach_fixture["candidates"] if c.full_name == "Ada Okonkwo")
            enroll_candidates(session, sequence=seq, candidate_ids=[ada.id])
            session.commit()
            summary = run_due(session, org_id=seq.org_id)
            session.commit()
            assert summary.sent == 1 and len(fake.sent) == 1
            msg = session.query(Message).filter_by(direction="outbound").one()
            assert (
                msg.mailbox_id == conn.id
                and msg.provider_native_id == "s1"
                and msg.conversation_id == "tnew"
            )
            enrollment = session.query(Enrollment).filter_by(candidate_id=ada.id).one()
            assert enrollment.current_step == 1

            # Ada answers in Gmail, on the same thread. The sync matches it to
            # our message and stops the sequence -- the reason replies sync at all.
            from juicebox.integrations.sync import sync_mailbox

            sync_mailbox(session, conn)  # first pass baselines
            ada_email = (ada.emails or ["ada@example.com"])[0]
            fake.messages["reply"] = message(
                "reply",
                sender=f"Ada Okonkwo <{ada_email}>",
                to="sathya@goforay.io",
                thread="tnew",
                ts_ms=int(time.time() * 1000),
                headers={
                    "In-Reply-To": msg.internet_message_id,
                    "References": msg.internet_message_id,
                },
            )
            fake.history_pages = [
                {
                    "historyId": "1100",
                    "history": [{"messagesAdded": [{"message": {"id": "reply"}}]}],
                }
            ]
            summary = sync_mailbox(session, conn)
            session.commit()
            assert summary.matched == 1
            session.refresh(enrollment)
            assert enrollment.status == "replied"
        finally:
            from juicebox.integrations.providers import reset_providers

            reset_providers()

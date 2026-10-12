"""Reading a seed inbox: where did our test email land?

Written from the failure list in juicebox/deliverability/README.md (items
10-16) before the reader existed. The fake server below speaks just enough of
imaplib's surface to answer the reader, and refuses every call that could
change a mailbox -- reading a seed must never train its spam filter.
"""

from __future__ import annotations

import imaplib
import re
from datetime import UTC, datetime, timedelta

import pytest

from juicebox.deliverability.seeds import (
    ImapReader,
    Probe,
    SeedReadError,
)

SENT = datetime(2026, 10, 10, 16, 0, tzinfo=UTC)


def _internaldate(when: datetime) -> str:
    return when.strftime("%d-%b-%Y %H:%M:%S +0000")


class FakeImap:
    """A tiny in-memory IMAP server with imaplib's call shapes.

    ``folders`` maps a folder name to (flags, [message, ...]); a message is a
    dict with from / subject / message_id / received / category.
    """

    def __init__(self, folders, *, gmail=False, password="app-pass"):
        self.folders = folders
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1") if gmail else ("IMAP4REV1",)
        self.password = password
        self.selected: str | None = None
        self.select_calls: list[tuple[str, bool]] = []
        self.fetch_parts: list[str] = []
        self.logged_out = False

    # -- connection ---------------------------------------------------------
    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"logged in"]

    def logout(self):
        self.logged_out = True
        return "BYE", [b""]

    def list(self, directory='""', pattern="*"):
        lines = []
        for name, (flags, _messages) in self.folders.items():
            flag_text = " ".join(flags)
            lines.append(f'({flag_text}) "/" "{name}"'.encode())
        return "OK", lines

    def select(self, mailbox="INBOX", readonly=False):
        name = mailbox.strip('"')
        self.select_calls.append((name, readonly))
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name][1])).encode()]

    # -- reading ------------------------------------------------------------
    def _messages(self):
        return self.folders[self.selected][1]

    def search(self, charset, *criteria):
        msgs = self._messages()
        tokens = list(criteria)
        hits = []
        for index, msg in enumerate(msgs, start=1):
            if self._matches(msg, tokens):
                hits.append(str(index))
        return "OK", [" ".join(hits).encode()]

    def _matches(self, msg, tokens):
        i = 0
        while i < len(tokens):
            key = tokens[i].upper()
            if key == "HEADER":
                field, value = tokens[i + 1], tokens[i + 2].strip('"')
                assert field.lower() == "message-id"
                if msg["message_id"].strip("<>") != value.strip("<>"):
                    return False
                i += 3
            elif key == "FROM":
                if tokens[i + 1].strip('"').lower() not in msg["from"].lower():
                    return False
                i += 2
            elif key == "SINCE":
                since = datetime.strptime(tokens[i + 1], "%d-%b-%Y").date()
                if msg["received"].date() < since:
                    return False
                i += 2
            elif key == "X-GM-RAW":
                assert "X-GM-EXT-1" in self.capabilities, "X-GM-RAW sent to a non-Gmail server"
                raw = tokens[i + 1].strip('"')
                wanted = re.search(r"rfc822msgid:(\S+)", raw)
                if wanted and msg["message_id"].strip("<>") != wanted.group(1).strip("<>"):
                    return False
                category = re.search(r"category:(\w+)", raw)
                if category and msg.get("category", "primary") != category.group(1):
                    return False
                i += 2
            else:
                raise AssertionError(f"unexpected search key {key}")
        return True

    def fetch(self, message_set, parts):
        self.fetch_parts.append(parts)
        out = []
        msgs = self._messages()
        for num in (
            message_set.decode().split(",")
            if isinstance(message_set, bytes)
            else str(message_set).split(",")
        ):
            msg = msgs[int(num) - 1]
            header = (
                f"From: Sender <{msg['from']}>\r\n"
                f"Subject: {msg['subject']}\r\n"
                f"Message-ID: {msg['message_id']}\r\n\r\n"
            ).encode()
            prefix = (
                f'{num} (INTERNALDATE "{_internaldate(msg["received"])}" '
                f"BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)] {{{len(header)}}}"
            ).encode()
            out.append((prefix, header))
            out.append(b")")
        return "OK", out

    # -- anything that writes is a test failure -----------------------------
    def _refuse(self, *args, **kwargs):
        raise AssertionError("the seed reader must never modify a seed mailbox")

    store = copy = move = expunge = append = create = delete = _refuse

    def uid(self, command, *args):
        if command.upper() in {"STORE", "COPY", "MOVE", "EXPUNGE"}:
            self._refuse()
        raise AssertionError("reader should not need UID commands")


def _msg(
    subject="Senior Backend Engineer at Acme",
    *,
    sender="ryan@goforay.io",
    message_id="<abc123@zoho.com>",
    received=SENT + timedelta(minutes=1),
    category="primary",
):
    return {
        "from": sender,
        "subject": subject,
        "message_id": message_id,
        "received": received,
        "category": category,
    }


def _reader(server):
    return ImapReader(
        host="imap.example.com",
        port=993,
        username="seed@example.com",
        password="app-pass",
        factory=lambda host, port, timeout=None: server,
    )


def _probe(**overrides):
    values = {
        "from_address": "ryan@goforay.io",
        "subject": "Senior Backend Engineer at Acme",
        "message_id": "<abc123@zoho.com>",
        "not_before": SENT,
    }
    values.update(overrides)
    return Probe(**values)


GENERIC = lambda inbox=(), junk=(): {  # noqa: E731
    "INBOX": (["\\HasNoChildren"], list(inbox)),
    "Bulk": (["\\HasNoChildren", "\\Junk"], list(junk)),
    "Sent": (["\\HasNoChildren", "\\Sent"], []),
}


class TestWhereItLanded:
    def test_inbox_by_message_id(self):
        server = FakeImap(GENERIC(inbox=[_msg()]))
        with _reader(server) as reader:
            result = reader.find(_probe())
        assert result.found and result.placement == "inbox"
        assert result.folder == "INBOX"

    def test_spam_found_through_the_special_use_flag(self):
        # Yahoo calls it Bulk; the \Junk flag is what identifies it.
        server = FakeImap(GENERIC(junk=[_msg()]))
        with _reader(server) as reader:
            result = reader.find(_probe())
        assert result.found and result.placement == "spam"
        assert result.folder == "Bulk"

    def test_gmail_spam_folder(self):
        server = FakeImap(
            {
                "INBOX": ([], []),
                "[Gmail]/Spam": (["\\HasNoChildren", "\\Junk"], [_msg()]),
                "[Gmail]/All Mail": (["\\All"], []),
            },
            gmail=True,
        )
        with _reader(server) as reader:
            result = reader.find(_probe())
        assert result.placement == "spam"

    def test_gmail_promotions_is_not_the_primary_inbox(self):
        server = FakeImap(
            {
                "INBOX": ([], [_msg(category="promotions")]),
                "[Gmail]/Spam": (["\\Junk"], []),
            },
            gmail=True,
        )
        with _reader(server) as reader:
            result = reader.find(_probe())
        assert result.found and result.placement == "other_tab"
        assert result.folder == "promotions"

    def test_gmail_primary_stays_inbox(self):
        server = FakeImap({"INBOX": ([], [_msg()]), "[Gmail]/Spam": (["\\Junk"], [])}, gmail=True)
        with _reader(server) as reader:
            assert reader.find(_probe()).placement == "inbox"

    def test_not_found_is_not_a_placement(self):
        server = FakeImap(GENERIC(inbox=[_msg(message_id="<other@x>", subject="Other")]))
        with _reader(server) as reader:
            result = reader.find(_probe())
        assert not result.found
        assert result.placement == ""


class TestMatching:
    def test_falls_back_to_sender_and_subject_without_a_message_id(self):
        server = FakeImap(GENERIC(inbox=[_msg(message_id="<rewritten@mx>")]))
        with _reader(server) as reader:
            result = reader.find(_probe(message_id=""))
        assert result.placement == "inbox"

    def test_falls_back_when_the_provider_rewrote_the_message_id(self):
        server = FakeImap(GENERIC(junk=[_msg(message_id="<rewritten@mx>")]))
        with _reader(server) as reader:
            assert reader.find(_probe()).placement == "spam"

    def test_yesterdays_test_with_the_same_subject_is_not_todays(self):
        old = _msg(message_id="<old@mx>", received=SENT - timedelta(days=1))
        server = FakeImap(GENERIC(inbox=[old]))
        with _reader(server) as reader:
            assert not reader.find(_probe(message_id="")).found

    def test_another_email_from_the_same_sender_is_not_a_match(self):
        server = FakeImap(GENERIC(inbox=[_msg(subject="Quick question", message_id="<q@mx>")]))
        with _reader(server) as reader:
            assert not reader.find(_probe(message_id="")).found

    def test_encoded_subjects_still_match(self):
        encoded = "=?UTF-8?B?U2VuaW9yIEJhY2tlbmQgRW5naW5lZXIgYXQgQWNtZQ==?="
        server = FakeImap(GENERIC(inbox=[_msg(subject=encoded, message_id="<x@mx>")]))
        with _reader(server) as reader:
            assert reader.find(_probe(message_id="")).placement == "inbox"


class TestTheSeedIsNeverTouched:
    def test_folders_are_opened_read_only_and_bodies_peeked(self):
        server = FakeImap(GENERIC(junk=[_msg(message_id="<x@mx>")]))
        with _reader(server) as reader:
            reader.find(_probe(message_id=""))
        assert server.select_calls, "nothing was opened"
        assert all(readonly for _name, readonly in server.select_calls)
        assert server.fetch_parts and all("PEEK" in p for p in server.fetch_parts)
        assert server.logged_out


class TestBadSeeds:
    def test_a_seed_without_a_spam_folder_still_reports_inbox_hits(self):
        server = FakeImap({"INBOX": ([], [_msg()])})
        with _reader(server) as reader:
            assert reader.spam_visible is False
            assert reader.find(_probe()).placement == "inbox"

    def test_a_failed_login_raises_without_the_password(self):
        server = FakeImap(GENERIC(), password="the-real-one")
        with pytest.raises(SeedReadError) as caught, _reader(server):
            pass
        assert "app-pass" not in str(caught.value)
        assert "sign in" in str(caught.value).lower()

    def test_an_unreachable_host_is_a_read_error(self):
        def refuse(host, port, timeout=None):
            raise OSError("connection refused")

        reader = ImapReader(
            host="imap.nowhere.example", port=993, username="u", password="p", factory=refuse
        )
        with pytest.raises(SeedReadError), reader:
            pass

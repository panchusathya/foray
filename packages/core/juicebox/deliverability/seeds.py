"""Seed inboxes: log in, find one test email, say which folder it is in.

A seed is an inbox we own at a mailbox provider whose filter we want to sample
(Gmail, Yahoo, anything that speaks IMAP). The reader is the only code that
talks to it, and it is read-only on purpose: folders are opened with EXAMINE
(``readonly=True``) and headers fetched with ``BODY.PEEK``, so nothing is
marked read, flagged or moved. Rescuing a test from spam would teach that
provider's filter that our mail is wanted, and every later test would read
better than real recipients see it.

Placements:

* ``inbox``     -- the inbox proper (Gmail: the Primary tab).
* ``other_tab`` -- delivered, but not where a person looks first: Gmail's
  Promotions / Updates / Social / Forums tabs. ``folder`` names the tab.
* ``spam``      -- the provider's junk folder (``\\Junk``; Gmail's
  ``[Gmail]/Spam``, Yahoo's ``Bulk``).

Not finding the message is not a placement. Whether "not found" means
``missing`` is the caller's decision (probes.py), and only after 24 hours.
"""

from __future__ import annotations

import contextlib
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parseaddr

#: provider_label -> (host, port). Anything else needs its host typed in.
IMAP_PRESETS: dict[str, tuple[str, int]] = {
    "gmail": ("imap.gmail.com", 993),
    "yahoo": ("imap.mail.yahoo.com", 993),
}

PLACEMENTS = ("inbox", "other_tab", "spam")

# Gmail tabs that are delivered-but-not-Primary, asked in this order.
_GMAIL_TABS = ("promotions", "updates", "social", "forums")
_SPAM_NAMES = (
    "[gmail]/spam",
    "[google mail]/spam",
    "spam",
    "junk",
    "bulk",
    "junk e-mail",
    "junk email",
    "bulk mail",
)
# How far before the recorded send time a match may claim to have arrived.
# INTERNALDATE is the receiving server's clock; ours can disagree by a little.
_CLOCK_SLACK = timedelta(minutes=5)
_TIMEOUT = 20
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$')
_INTERNALDATE = re.compile(rb'INTERNALDATE "([^"]+)"')


class SeedReadError(Exception):
    """We could not read the seed: bad login, unreachable host, protocol error.

    Unknown, never "not delivered". Messages never carry the password.
    """


@dataclass(frozen=True)
class Probe:
    """What identifies one seed test inside the seed inbox."""

    from_address: str
    subject: str
    message_id: str = ""
    not_before: datetime | None = None


@dataclass(frozen=True)
class Placement:
    found: bool
    placement: str = ""
    folder: str = ""


def _decode(value: str) -> str:
    try:
        text = str(make_header(decode_header(value or "")))
    except (UnicodeDecodeError, LookupError, ValueError):
        text = value or ""
    return " ".join(text.split()).casefold()


def _quote(name: str) -> str:
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _bare_id(message_id: str) -> str:
    return (message_id or "").strip().strip("<>")


class ImapReader:
    """One IMAP session against one seed. Use as a context manager."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str = "",
        access_token: str = "",
        factory=None,
        timeout: int = _TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        # An OAuth bearer token (Yahoo seeds). It is sent with AUTHENTICATE
        # XOAUTH2, never LOGIN: a token is not a password, and a server that
        # logs failed LOGINs would be logging a live credential.
        self.access_token = access_token
        self._factory = factory or (
            lambda h, p, timeout=None: imaplib.IMAP4_SSL(h, p, timeout=timeout)
        )
        self._timeout = timeout
        self._conn = None
        self._spam_folder: str | None = None
        self.gmail = False

    # -- session --------------------------------------------------------------
    def __enter__(self) -> ImapReader:
        try:
            self._conn = self._factory(self.host, self.port, timeout=self._timeout)
        except (TimeoutError, OSError, imaplib.IMAP4.error) as exc:
            raise SeedReadError(
                f"Could not reach {self.host}:{self.port} ({type(exc).__name__})"
            ) from None
        try:
            if self.access_token:
                self._xoauth2()
            else:
                self._conn.login(self.username, self._password)
        except imaplib.IMAP4.error:
            self._safe_logout()
            if self.access_token:
                raise SeedReadError(
                    f"The mail server refused the sign-in for {self.username}: reconnect this seed"
                ) from None
            raise SeedReadError(
                f"Could not sign in to {self.username}: check the address and app password"
            ) from None
        except (TimeoutError, OSError) as exc:
            self._safe_logout()
            raise SeedReadError(
                f"Lost the connection to {self.host} while signing in ({type(exc).__name__})"
            ) from None
        caps = getattr(self._conn, "capabilities", ()) or ()
        self.gmail = any(str(c).upper() == "X-GM-EXT-1" for c in caps)
        self._spam_folder = self._find_spam_folder()
        return self

    def __exit__(self, *exc) -> bool:
        self._safe_logout()
        return False

    def _xoauth2(self) -> None:
        sasl = f"user={self.username}\x01auth=Bearer {self.access_token}\x01\x01".encode()
        sent = []

        def answer(_challenge: bytes) -> bytes:
            # On failure the server sends a JSON error as a second challenge
            # and expects an empty reply before it says NO.
            if sent:
                return b""
            sent.append(True)
            return sasl

        self._conn.authenticate("XOAUTH2", answer)

    def _safe_logout(self) -> None:
        if self._conn is None:
            return
        # A dead socket on the way out is fine.
        with contextlib.suppress(Exception):
            self._conn.logout()
        self._conn = None

    @property
    def spam_visible(self) -> bool:
        return self._spam_folder is not None

    # -- folders --------------------------------------------------------------
    def _find_spam_folder(self) -> str | None:
        try:
            status, lines = self._conn.list()
        except (imaplib.IMAP4.error, OSError):
            return None
        if status != "OK":
            return None
        named = None
        for line in lines or []:
            if not isinstance(line, bytes):
                continue
            match = _LIST_LINE.match(line.strip())
            if not match:
                continue
            flags = match.group("flags").decode(errors="replace").lower().split()
            name = match.group("name").decode(errors="replace").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1]
            if "\\noselect" in flags:
                continue
            if "\\junk" in flags:
                return name
            if named is None and name.lower() in _SPAM_NAMES:
                named = name
        return named

    def _open(self, folder: str) -> bool:
        try:
            status, _ = self._conn.select(_quote(folder), readonly=True)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise SeedReadError(f"Could not open {folder} ({type(exc).__name__})") from None
        return status == "OK"

    def _search(self, *criteria: str) -> list[bytes]:
        try:
            status, data = self._conn.search(None, *criteria)
        except imaplib.IMAP4.error:
            return []
        except OSError as exc:
            raise SeedReadError(f"Search failed ({type(exc).__name__})") from None
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    # -- matching -------------------------------------------------------------
    def _by_message_id(self, message_id: str) -> list[bytes]:
        bare = _bare_id(message_id)
        if not bare:
            return []
        if self.gmail:
            return self._search("X-GM-RAW", f'"rfc822msgid:{bare}"')
        return self._search("HEADER", "Message-ID", f'"<{bare}>"')

    def _by_sender_and_subject(self, probe: Probe) -> list[tuple[bytes, str]]:
        """(sequence number, Message-ID) of messages that really are this probe."""
        since = (probe.not_before or datetime.now(UTC)) - _CLOCK_SLACK
        nums = self._search("FROM", f'"{probe.from_address}"', "SINCE", since.strftime("%d-%b-%Y"))
        if not nums:
            return []
        want_subject = _decode(probe.subject)
        want_from = (probe.from_address or "").strip().lower()
        try:
            status, data = self._conn.fetch(
                b",".join(nums),
                "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)])",
            )
        except imaplib.IMAP4.error:
            return []
        except OSError as exc:
            raise SeedReadError(f"Fetch failed ({type(exc).__name__})") from None
        if status != "OK":
            return []
        hits = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            prefix, raw = item[0], item[1]
            num = prefix.split(b" ", 1)[0]
            headers = message_from_bytes(raw)
            if parseaddr(headers.get("From", ""))[1].strip().lower() != want_from:
                continue
            if _decode(headers.get("Subject", "")) != want_subject:
                continue
            arrived = _arrival(prefix)
            if probe.not_before and arrived and arrived < probe.not_before - _CLOCK_SLACK:
                continue
            hits.append((num, headers.get("Message-ID", "")))
        return hits

    def _locate(self, probe: Probe) -> tuple[bool, str]:
        """In the open folder: (found, the Message-ID the server holds)."""
        if self._by_message_id(probe.message_id):
            return True, probe.message_id
        hits = self._by_sender_and_subject(probe)
        if hits:
            return True, hits[-1][1] or probe.message_id
        return False, ""

    def _gmail_tab(self, message_id: str) -> str:
        bare = _bare_id(message_id)
        if not bare:
            return ""
        for tab in _GMAIL_TABS:
            if self._search("X-GM-RAW", f'"rfc822msgid:{bare} category:{tab}"'):
                return tab
        return ""

    def find(self, probe: Probe) -> Placement:
        if self._conn is None:
            raise SeedReadError("reader used outside its session")
        if self._open("INBOX"):
            found, held_id = self._locate(probe)
            if found:
                if self.gmail:
                    tab = self._gmail_tab(held_id)
                    if tab:
                        return Placement(found=True, placement="other_tab", folder=tab)
                return Placement(found=True, placement="inbox", folder="INBOX")
        if self._spam_folder and self._open(self._spam_folder):
            found, _ = self._locate(probe)
            if found:
                return Placement(found=True, placement="spam", folder=self._spam_folder)
        return Placement(found=False)


def _arrival(prefix: bytes) -> datetime | None:
    match = _INTERNALDATE.search(prefix or b"")
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


# -- which reader a seed gets ---------------------------------------------------


def _default_reader(seed):
    from ..integrations.crypto import decrypt_json

    if seed.kind in ("gmail_api", "imap_oauth"):
        from .seed_oauth import reader_for_oauth_seed

        return reader_for_oauth_seed(seed)
    if seed.kind != "imap":
        raise SeedReadError(f"{seed.kind} seeds are not supported yet")
    try:
        secret = decrypt_json(seed.secret_blob) if seed.secret_blob else {}
    except ValueError:
        raise SeedReadError("Stored password could not be decrypted; re-add this seed") from None
    return ImapReader(
        host=seed.imap_host,
        port=seed.imap_port or 993,
        username=seed.username or seed.address,
        password=secret.get("password", ""),
    )


# Tests replace this; everything that reads a seed goes through reader_for.
_reader_factory = _default_reader


def reader_for(seed):
    return _reader_factory(seed)

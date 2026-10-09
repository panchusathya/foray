"""The mail-provider seam: what a connected mailbox must be able to do.

Outlook was the only provider for long enough that its JSON shape leaked into
the sync path. Zoho Mail does not have Graph's delta queries, does not accept
custom headers, calls a conversation a ``threadId``, and returns millisecond
epochs where Graph returns ISO strings. Feeding those payloads through the
Graph helpers would have meant a second, subtly different sync.

So providers own three things and nothing else: OAuth, sending, and reading
raw mailbox traffic. Everything downstream of that -- matching a reply to the
outreach that provoked it, classifying intent, stopping enrollments, deciding
whether an email belongs in the CRM at all, generating reply-draft bodies --
stays in sync.py / sequencer.py / reply_drafting.py, working on the
``MailboxEvent`` DTO below rather than on any provider's wire format.

Adding a provider means implementing ``MailProvider`` and registering it. It
must not mean touching the sequencer.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

# Folder names the sync layer asks for, mapped by each provider to its own ids.
FOLDER_INBOX = "inbox"
FOLDER_SENT = "sent"


class ProviderError(Exception):
    """Any provider-side failure. Concrete providers subclass this so the sync
    loop can survive one broken mailbox without knowing which provider broke."""

    def __init__(self, detail: str, *, status_code: int = 0) -> None:
        super().__init__(detail)
        self.status_code = status_code


@dataclass(frozen=True)
class MailboxEvent:
    """One message as the sync layer sees it, whatever provider produced it.

    ``native_id`` is the provider's own message id and lands on
    ``Message.provider_native_id`` -- the column dedupe, reply-draft lookup and
    the partial unique index all key on. A provider that cannot supply one has
    no way to be idempotent, so it is required rather than optional.
    """

    native_id: str
    from_address: str
    subject: str = ""
    body: str = ""
    body_is_html: bool = False
    to_addresses: tuple[str, ...] = ()
    # Graph conversationId | Zoho threadId | Gmail threadId. Same role: groups a thread.
    thread_id: str = ""
    # RFC 5322 Message-ID of this message, and of the one it answers.
    rfc_message_id: str = ""
    in_reply_to: str = ""
    # Where the sender wants an answer to go. ATS mail is sent *from* a robot
    # and puts the address that routes back here, so for the application inbox
    # this -- not `from_address` -- is what a reply targets.
    reply_to: str = ""
    received_at: datetime | None = None
    sent_at: datetime | None = None
    is_draft: bool = False
    removed: bool = False
    web_link: str = ""
    has_attachments: bool = False

    @property
    def occurred_at(self) -> datetime | None:
        """What to store as the message's timestamp. Graph gives both; a sent
        item's sentDateTime beats the time it landed back in our view of it."""
        return self.sent_at or self.received_at


@dataclass
class FolderPage:
    """One read of one folder.

    ``next_cursor`` is opaque to the caller and is only meaningful to the
    provider that produced it. The caller commits it *after* recording every
    event in the page (see ``commit_cursor``), so a crash mid-page replays
    rather than skipping -- which the idempotent ingest makes free.
    """

    events: list[MailboxEvent] = field(default_factory=list)
    next_cursor: str = ""
    # True when the call only established a baseline and deliberately read no
    # history. Old mail is not outreach traffic.
    initialized: bool = False
    # Zoho pages by offset: more remains after this cursor is committed.
    has_more: bool = False


@dataclass
class SweepRecord:
    """One sent message, reduced to what a discovery sweep may keep.

    Deliberately a different DTO from ``MailboxEvent``, and deliberately
    missing ``subject`` and ``body``. The tray holds evidence about people who
    have never been reviewed, let alone opted in, so the guarantee is
    structural rather than a habit: there is no field to put content in, and
    both providers build these from raw payloads instead of ``to_event`` --
    which fills ``body`` from a preview and merges Cc into the To list.
    """

    native_id: str
    from_address: str
    to_addresses: tuple[str, ...] = ()
    # Kept apart from To: a 40-person Cc is not a relationship, and an address
    # only ever Cc'd is a weaker signal than one written to directly.
    cc_addresses: tuple[str, ...] = ()
    thread_id: str = ""
    sent_at: datetime | None = None
    is_draft: bool = False
    # address -> the display name the header carried, e.g.
    # {"adia@acme.com": "Adia Chenumbay"}. Both providers know this and both
    # used to discard it, leaving discovery to guess a name from the local
    # part -- which only ever works for first.last and mangles or invents the
    # rest. A name on a To: line is identity, not message content.
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class SweepPage:
    """One page of a historical sweep.

    ``next_cursor`` is opaque and belongs in ``discovery_state``, never in the
    live delta columns -- overwriting one of those makes the ongoing sync
    permanently re-read or permanently skip mail.
    """

    records: list[SweepRecord] = field(default_factory=list)
    next_cursor: str = ""
    has_more: bool = False


@dataclass
class ConnectResult:
    """Everything the callback needs to persist a new connection.

    ``api_base`` / ``auth_base`` exist because Zoho resolves the data centre
    during the callback, not before it: the token exchange has to go to the
    ``accounts-server`` the redirect came back with, and every later refresh to
    the same host. Microsoft leaves both empty and uses its module constants.
    """

    tokens: dict
    address: str
    display_name: str = ""
    provider_account_id: str = ""
    api_base: str = ""
    auth_base: str = ""
    sync_state: dict = field(default_factory=dict)


@dataclass
class DraftResult:
    provider_draft_id: str = ""
    web_link: str = ""


@runtime_checkable
class MailProvider(Protocol):
    name: str

    def is_configured(self) -> bool:
        """False when the deployment has no credentials for this provider."""
        ...

    def redirect_uri(self) -> str: ...

    def authorize_url(self, state: str) -> str: ...

    def exchange_code(self, code: str, params: Mapping[str, str]) -> ConnectResult:
        """Trade the callback's code for tokens and identity. ``params`` is the
        full callback query string -- Zoho needs ``accounts-server`` from it."""
        ...

    def build_sender(self, db, connection):
        """An ``outreach.sender.EmailSender`` bound to this mailbox."""
        ...

    def read_cursor(self, connection, folder: str) -> str:
        """Where this folder's last pass stopped. Opaque to the caller: a Graph
        delta link, a Zoho high-water mark, whatever the provider needs."""
        ...

    def iter_folder(self, db, connection, folder: str, *, cursor: str) -> FolderPage: ...

    def commit_cursor(self, db, connection, folder: str, cursor: str) -> None: ...

    def place_reply_draft(self, db, *, connection, inbound, html_body: str) -> DraftResult:
        """Put an already-generated reply body in the mailbox's own Drafts, on
        the inbound message's thread. Body generation is not the provider's job."""
        ...

    def search_participant(
        self, db, connection, address: str
    ) -> Iterable[MailboxEvent]: ...

    def iter_sent_history(
        self, db, connection, *, since: datetime, cursor: str = ""
    ) -> SweepPage:
        """Walk Sent Items backwards to ``since``, for contact discovery.

        Sent Items only, and no folder argument: twelve months of Inbox is
        typically 10-50x the volume and is mostly newsletters and inbound
        recruiters, so a sweep that could be pointed at it would eventually be
        pointed at it. "Everyone we have reached out to" is a Sent premise.

        The cursor is a *separate* history cursor. Implementations must not
        read or write ``inbox_delta_link``, ``sent_delta_link`` or Zoho's
        ``sync_state["cursors"]``; the caller keeps this one in
        ``discovery_state``.

        Metadata only -- see ``SweepRecord``. Bodies must never be fetched,
        not merely discarded after fetching.
        """
        ...


_providers: dict[str, MailProvider] = {}


def _load_defaults() -> None:
    from .gmail import GmailProvider
    from .microsoft import MicrosoftProvider
    from .zoho import ZohoProvider

    _providers["gmail"] = GmailProvider()
    _providers["microsoft"] = MicrosoftProvider()
    _providers["zoho"] = ZohoProvider()


def get_provider(name: str) -> MailProvider:
    """Look up a provider by ``MailboxConnection.provider``.

    An unknown name is a hard error, never a silent default: falling back to
    Microsoft here would send from the wrong mailbox, and falling back to a
    no-op would drop replies without a trace.
    """
    if not _providers:
        _load_defaults()
    try:
        return _providers[name]
    except KeyError:
        raise ProviderError(f"unknown mail provider {name!r}") from None


def provider_names() -> list[str]:
    if not _providers:
        _load_defaults()
    return sorted(_providers)


def set_provider(name: str, provider: MailProvider | None) -> None:
    """Tests swap a provider in; None removes it."""
    if not _providers:
        _load_defaults()
    if provider is None:
        _providers.pop(name, None)
    else:
        _providers[name] = provider


def reset_providers() -> None:
    _providers.clear()

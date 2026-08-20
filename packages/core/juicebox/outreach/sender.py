"""Email delivery providers.

``console`` is the default so nothing leaves the machine during development;
``memory`` is what tests assert against; ``smtp`` is real delivery.
"""

from __future__ import annotations

import logging
import smtplib
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from typing import Protocol

from ..config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OutboundEmail:
    to: str
    subject: str
    text_body: str
    html_body: str
    from_email: str
    from_name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # RFC 5322 threading hints, set for follow-up steps from the previous
    # outbound message on the same enrollment. Providers that cannot express
    # them ignore them and send a fresh message -- a broken thread is worth
    # far less than a step that refuses to send.
    in_reply_to: str = ""
    references: str = ""

    @property
    def from_header(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""
    # Set by mailbox providers; reply detection matches on these.
    # provider_native_id is the provider's own message id and is what makes
    # ingest idempotent. conversation_id is Graph's conversationId or Zoho's
    # threadId. internet_message_id is the RFC Message-ID, which is what lets
    # a later reply be matched by its In-Reply-To header.
    provider_native_id: str = ""
    internet_message_id: str = ""
    conversation_id: str = ""


class EmailSender(Protocol):
    name: str

    def send(self, email: OutboundEmail) -> SendResult:  # pragma: no cover - protocol
        ...


class ConsoleSender:
    """Logs the message instead of sending it."""

    name = "console"

    def send(self, email: OutboundEmail) -> SendResult:
        logger.info(
            "[console-email] to=%s from=%s subject=%s\n%s",
            email.to,
            email.from_header,
            email.subject,
            email.text_body,
        )
        return SendResult(ok=True, provider_message_id=f"console-{uuid.uuid4().hex[:16]}")


class MemorySender:
    """Captures messages in a list — used by tests and dry runs."""

    name = "memory"

    def __init__(self) -> None:
        self.outbox: list[OutboundEmail] = []

    def send(self, email: OutboundEmail) -> SendResult:
        self.outbox.append(email)
        return SendResult(ok=True, provider_message_id=f"memory-{len(self.outbox)}")

    def clear(self) -> None:
        self.outbox.clear()


class SmtpSender:
    name = "smtp"

    def __init__(
        self, host: str, port: int, user: str, password: str, *, starttls: bool = True
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.starttls = starttls

    def _build(self, email: OutboundEmail) -> MimeMessage:
        message = MimeMessage()
        message["To"] = email.to
        message["From"] = email.from_header
        message["Subject"] = email.subject
        if email.in_reply_to:
            message["In-Reply-To"] = email.in_reply_to
        if email.references:
            message["References"] = email.references
        for key, value in email.headers.items():
            message[key] = value
        message.set_content(email.text_body)
        if email.html_body:
            message.add_alternative(email.html_body, subtype="html")
        return message

    def send(self, email: OutboundEmail) -> SendResult:
        message = self._build(email)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                if self.starttls:
                    server.starttls()
                if self.user:
                    server.login(self.user, self.password)
                server.send_message(message)
        except Exception as exc:  # noqa: BLE001 - surfaced on the Message row
            logger.error("smtp send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        return SendResult(ok=True, provider_message_id=message.get("Message-ID", "") or "smtp")


_sender: EmailSender | None = None


def get_sender() -> EmailSender:
    global _sender
    if _sender is None:
        settings = get_settings()
        provider = settings.email_provider
        if provider == "smtp" and settings.smtp_host:
            _sender = SmtpSender(
                settings.smtp_host,
                settings.smtp_port,
                settings.smtp_user,
                settings.smtp_password,
                starttls=settings.smtp_starttls,
            )
        elif provider == "memory":
            _sender = MemorySender()
        else:
            _sender = ConsoleSender()
    return _sender


def set_sender(sender: EmailSender | None) -> None:
    """Tests and the dry-run path swap the provider in directly."""
    global _sender
    _sender = sender


class FailingSender:
    """Stands in when a sequence's mailbox can't send; every send fails loudly.

    Falling back to the global provider here would silently send from the
    wrong address — the user chose a specific mailbox.
    """

    name = "failing"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def send(self, email: OutboundEmail) -> SendResult:
        return SendResult(ok=False, error=self.reason)


_sender_factories: dict[str, object] = {}


def set_sender_factory(provider: str, factory) -> None:
    """Tests inject a fake sender for one provider; None removes it."""
    if factory is None:
        _sender_factories.pop(provider, None)
    else:
        _sender_factories[provider] = factory


def reset_sender_factories() -> None:
    _sender_factories.clear()


def has_sender_factory(provider: str) -> bool:
    return provider in _sender_factories


@dataclass
class ResolvedSender:
    """Who is about to send, and whether anybody can right now.

    ``deferred`` is not a failure: every eligible mailbox is at its daily cap,
    so the caller should leave the enrollment alone and let the job requeue.
    Turning that into an error would park a permanent idempotency key and the
    step could never send again.
    """

    sender: EmailSender
    connection: object | None = None  # MailboxConnection; None = global provider
    deferred: bool = False
    reason: str = ""


def sender_for_connection(db, connection) -> EmailSender:
    factory = _sender_factories.get(connection.provider)
    if factory is not None:
        return factory(db, connection)

    from ..integrations.providers import get_provider

    return get_provider(connection.provider).build_sender(db, connection)


def _bind(db, org_id: str, mailbox_id: str, *, now) -> ResolvedSender:
    """Resolve one specific mailbox, refusing loudly when it cannot send.

    Never falls back to the global provider: the address was chosen (pinned to
    the enrollment, or fixed on the sequence), and sending from a different one
    is worse than not sending.
    """
    from ..models import MailboxConnection

    connection = db.get(MailboxConnection, mailbox_id)
    if connection is None or connection.org_id != org_id:
        return ResolvedSender(FailingSender("mailbox connection not found"))
    if connection.status != "active":
        return ResolvedSender(
            FailingSender(f"mailbox {connection.address} is {connection.status}; reconnect it"),
            connection=connection,
        )
    if not connection.send_enabled:
        return ResolvedSender(
            FailingSender(f"sending is switched off for mailbox {connection.address}"),
            connection=connection,
        )

    from .rotation import is_over_cap

    if is_over_cap(db, connection=connection, now=now):
        return ResolvedSender(
            FailingSender(f"mailbox {connection.address} is at its daily cap"),
            connection=connection,
            deferred=True,
            reason="mailbox_cap",
        )
    return ResolvedSender(sender_for_connection(db, connection), connection=connection)


def preview_connection(db, sequence, enrollment=None, *, now=None):
    """Which mailbox a preview should render as, or None for the global provider.

    Deliberately not ``resolve_sender``. That function answers "can this send go
    out right now", so it returns no connection when every mailbox is at its cap
    -- and a preview that fell back to the configured default would then show
    the wrong signature precisely when the pool was busy. A cap decides *when* a
    send happens, not *who* it comes from.

    It also ignores status: a preview of a sequence pinned to a revoked mailbox
    should still say which mailbox, because that is the fact the recruiter needs
    to see in order to fix it.
    """
    from ..models import MailboxConnection, utcnow
    from .rotation import active_mailboxes, pick_mailbox

    now = now or utcnow()

    def _owned(mailbox_id):
        connection = db.get(MailboxConnection, mailbox_id) if mailbox_id else None
        if connection is not None and connection.org_id == sequence.org_id:
            return connection
        return None

    pinned = _owned(getattr(enrollment, "mailbox_id", None))
    if pinned is not None:
        return pinned
    if getattr(sequence, "sender_mode", "rotate") == "fixed":
        return _owned(sequence.mailbox_id)

    # Rotating: show whoever would send if this went out now. Falling back to
    # any live mailbox when all are capped keeps the preview truthful about the
    # kind of signature the candidate will see.
    chosen = pick_mailbox(db, org_id=sequence.org_id, now=now)
    if chosen is not None:
        return chosen
    live = active_mailboxes(db, org_id=sequence.org_id)
    return live[0] if live else None


def resolve_sender(db, sequence, enrollment=None, *, now=None) -> ResolvedSender:
    """Pick the mailbox for the next send on this sequence.

    Order matters:

    1. An enrollment that has already sent stays on its mailbox forever. The
       candidate's client threads on the address, so moving mid-sequence starts
       a second conversation from a stranger.
    2. A sequence pinned with sender_mode="fixed" uses exactly that mailbox.
    3. Otherwise rotate over the pool.

    With no mailbox connected at all, rotation falls through to the globally
    configured provider (console/smtp/memory), which is how every org behaved
    before rotation existed.
    """
    from ..models import utcnow

    now = now or utcnow()
    org_id = sequence.org_id

    if enrollment is not None and enrollment.mailbox_id:
        return _bind(db, org_id, enrollment.mailbox_id, now=now)

    if getattr(sequence, "sender_mode", "rotate") == "fixed":
        if not sequence.mailbox_id:
            # Pinned to nothing. Fall back rather than refuse, matching what a
            # null mailbox_id has always meant.
            return ResolvedSender(get_sender())
        return _bind(db, org_id, sequence.mailbox_id, now=now)

    from .rotation import active_mailboxes, pick_mailbox

    if not active_mailboxes(db, org_id=org_id):
        # This org has no mailbox connected at all, so there is no identity to
        # be wrong about: fall through to the globally configured provider.
        return ResolvedSender(get_sender())

    connection = pick_mailbox(db, org_id=org_id, now=now)
    if connection is None:
        # Mailboxes exist but none has room (or they are all switched off).
        # Deferring holds the send until one does; falling back to the global
        # provider would quietly send from the wrong address instead.
        return ResolvedSender(
            FailingSender("no connected mailbox has capacity right now"),
            deferred=True,
            reason="mailbox_cap",
        )
    return ResolvedSender(sender_for_connection(db, connection), connection=connection)

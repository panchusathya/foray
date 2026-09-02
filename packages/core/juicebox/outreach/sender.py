"""Email delivery providers.

``console`` is the default so nothing leaves the machine during development;
``memory`` is what tests assert against; ``smtp`` is real delivery.

``mailbox`` is real delivery through a connected mailbox's own HTTPS API, for
hosts that block outbound SMTP. It is the one provider ``get_sender()`` cannot
build, because choosing the connection needs a session and an org -- see
``sender_for_org``.
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
    # The parent's id in the SENDING provider's own namespace (a Graph message
    # id, a Zoho messageId), set only when the parent went out from the same
    # mailbox. Graph rejects In-Reply-To and References in
    # internetMessageHeaders -- it takes x- names only -- so the RFC hints
    # above cannot thread an Outlook send on their own; replying to this id is
    # the only way. Providers that thread on headers alone ignore it.
    thread_native_id: str = ""

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
        self.attachments: list[list] = []

    def send(self, email: OutboundEmail) -> SendResult:
        self.outbox.append(email)
        return SendResult(ok=True, provider_message_id=f"memory-{len(self.outbox)}")

    def send_with_attachments(self, email: OutboundEmail, attachments) -> SendResult:
        """Same capture, remembering files so tests can assert the resume travelled."""
        self.attachments.append(list(attachments or []))
        return self.send(email)

    def clear(self) -> None:
        self.outbox.clear()
        self.attachments.clear()


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
        return self._deliver(self._build(email))

    def send_with_attachments(self, email: OutboundEmail, attachments) -> SendResult:
        """Same send, with files. Only the application submitter needs this:
        outreach never attaches anything, and a resume that silently did not
        travel is the worst possible outcome for an application."""
        message = self._build(email)
        for item in attachments:
            maintype, _, subtype = (item.content_type or "application/octet-stream").partition("/")
            message.add_attachment(
                item.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=item.filename,
            )
        return self._deliver(message)

    def _deliver(self, message: MimeMessage) -> SendResult:
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


class ResendSender:
    """Transactional mail over HTTPS, from any address on a verified domain.

    Exists because the two things the alias relay needs are the two things the
    Zoho API refuses. Verified against live Zoho:

    * a sub-addressed ``From`` -- ``500 "Given FromAddress not exists!"``. Zoho
      will only send as an address configured on the account, and
      ``anna+<token>@`` is not one, so the per-application identity the whole
      alias design rests on could not survive the send.
    * ``Reply-To`` -- ``500 "You need to verify the ReplyTo address"``, which no
      employer address will ever be.

    It is also HTTPS on 443, which is the only way out of this host: outbound
    25, 465, 587 and 2525 are all silently dropped by the provider, so SmtpSender
    cannot deliver from production however correct its credentials are.

    Resend returns its own id and not the RFC ``Message-ID``, so
    ``internet_message_id`` stays empty. Nothing depends on it: inbound
    attribution keys off the alias sub-address, and threading at the employer's
    end is carried by the ``In-Reply-To`` we set from *their* message.
    """

    name = "resend"
    endpoint = "https://api.resend.com/emails"

    def __init__(self, api_key: str, *, client=None, timeout: float = 20.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    def _payload(self, email: OutboundEmail) -> dict:
        headers = dict(email.headers or {})
        # The RFC hints travel as headers here; Resend has no first-class field
        # for them and passes unknown headers through untouched.
        if email.in_reply_to:
            headers.setdefault("In-Reply-To", email.in_reply_to)
        if email.references:
            headers.setdefault("References", email.references)

        # Reply-To is a top-level field, not a header, and sending it in both
        # places produces two Reply-To lines on the wire.
        reply_to = headers.pop("Reply-To", "") or headers.pop("reply-to", "")

        payload = {
            "from": email.from_header,
            "to": [email.to],
            "subject": email.subject,
            "text": email.text_body,
        }
        if email.html_body:
            payload["html"] = email.html_body
        if reply_to:
            payload["reply_to"] = reply_to
        if headers:
            payload["headers"] = headers
        return payload

    def send(self, email: OutboundEmail) -> SendResult:
        return self._deliver(self._payload(email), to=email.to)

    def send_with_attachments(self, email: OutboundEmail, attachments) -> SendResult:
        """Same send, with files. Application mail without the resume is a
        worse outcome than not sending, which is why EmailSubmitter requires
        this method rather than falling through to a body-only send."""
        import base64

        payload = self._payload(email)
        payload["attachments"] = [
            {
                "filename": item.filename,
                "content": base64.b64encode(item.content).decode("ascii"),
                "content_type": item.content_type or "application/octet-stream",
            }
            for item in (attachments or [])
        ]
        return self._deliver(payload, to=email.to)

    def _deliver(self, payload: dict, *, to: str) -> SendResult:
        if not self.api_key:
            logger.error("resend send refused: RESEND_API_KEY is unset")
            return SendResult(ok=False, error="RESEND_API_KEY is unset")

        import httpx

        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(
                self.endpoint,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
        except Exception as exc:  # noqa: BLE001 - surfaced on the caller's row
            logger.error("resend send failed for %s: %s", to, exc)
            return SendResult(ok=False, error=str(exc)[:400])
        finally:
            if self._client is None:
                client.close()

        if response.status_code >= 400:
            # The body carries Resend's own reason -- an unverified domain, a
            # From the domain does not own. Keeping it is the difference
            # between a fixable error and "the mail did not arrive".
            detail = (response.text or "")[:400]
            logger.error("resend send failed for %s (%s): %s", to, response.status_code, detail)
            return SendResult(ok=False, error=f"resend {response.status_code}: {detail}")

        try:
            data = response.json()
        except ValueError:
            data = {}
        native_id = str((data or {}).get("id") or "")
        if not native_id:
            logger.warning("resend accepted the send to %s but returned no id", to)
        return SendResult(
            ok=True,
            provider_message_id=native_id or "resend",
            provider_native_id=native_id,
        )


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
        elif provider == "resend":
            # Unlike `mailbox`, this one needs no session: the identity is an
            # address on a verified domain, not a row in the database.
            _sender = ResendSender(settings.resend_api_key)
        elif provider == "mailbox":
            # `mailbox` cannot be served here: picking the connection needs a
            # session and an org, which this function has neither of. Falling
            # through to console would be the worst of both -- the deployment
            # is configured for real delivery, and the caller would be handed
            # a sender that reports every send as a success having only
            # logged it. `EmailSubmitter` does exactly that and would mark an
            # application submitted with nothing sent.
            logger.error(
                "EMAIL_PROVIDER=mailbox reached get_sender(); this caller needs "
                "sender_for_org(db, org_id). Refusing rather than sending to the log."
            )
            _sender = FailingSender(
                "EMAIL_PROVIDER=mailbox needs a session; this path must use sender_for_org()"
            )
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


def sender_for_org(db, org_id: str) -> EmailSender:
    """The sender for app-level mail: sign-in codes and the alias relay.

    Three senders exist for three different questions, and collapsing them is
    how mail goes to the wrong person:

    * ``get_sender()`` -- the process-wide provider. No session, so it cannot
      reach a connected mailbox.
    * ``resolve_sender()`` -- *outreach*. Rotation, daily caps, a pinned
      mailbox per enrollment.
    * this -- transactional mail the app itself owes somebody.

    It deliberately skips the daily cap. A cap is a campaign-volume control,
    and refusing a sign-in code because an outreach mailbox is busy would lock
    a candidate out of their own account.

    Falls back to the global provider unless ``mailbox`` mode is on, so a
    deployment that never sets it keeps exactly the behaviour it had.
    """
    settings = get_settings()
    if settings.email_provider != "mailbox":
        return get_sender()

    address = settings.email_mailbox_address
    if not address:
        logger.error("EMAIL_PROVIDER=mailbox but EMAIL_MAILBOX_ADDRESS is unset")
        return FailingSender("EMAIL_PROVIDER=mailbox but EMAIL_MAILBOX_ADDRESS is unset")

    from sqlalchemy import select

    from ..models import MailboxConnection

    connection = db.scalars(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.address == address,
        )
    ).first()

    # Every branch below logs. This mail is sent from paths that ignore the
    # result -- the OTP endpoint answers 202 either way, so that it cannot be
    # used to enumerate addresses -- and a misconfiguration that only showed up
    # as "the code never arrived" is exactly the failure this mode exists to
    # get us out of.
    if connection is None:
        logger.error("EMAIL_MAILBOX_ADDRESS=%s is not a connected mailbox in %s", address, org_id)
        return FailingSender(f"{address} is not a connected mailbox")
    if connection.status != "active":
        logger.error("mailbox %s is %s; reconnect it", address, connection.status)
        return FailingSender(f"mailbox {address} is {connection.status}; reconnect it")
    if not connection.send_enabled:
        logger.error("sending is switched off for mailbox %s", address)
        return FailingSender(f"sending is switched off for mailbox {address}")
    return sender_for_connection(db, connection)


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


def _rotation_subset(sequence) -> list[str]:
    """The mailboxes this sequence's rotation is limited to.

    Empty means the whole pool, which is what every sequence written before
    the subset existed carries -- and what an untouched one keeps, so a newly
    connected mailbox joins its rotation without an edit.
    """
    return list(getattr(sequence, "mailbox_ids", None) or [])


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

    # Rotating: show whoever would send if this went out now, within the
    # sequence's own subset. Falling back to any live mailbox when all are
    # capped keeps the preview truthful about the kind of signature the
    # candidate will see.
    only = _rotation_subset(sequence)
    chosen = pick_mailbox(db, org_id=sequence.org_id, now=now, only=only)
    if chosen is not None:
        return chosen
    live = active_mailboxes(db, org_id=sequence.org_id, only=only)
    if live:
        return live[0]
    # Nothing live to show. Under a subset, name the chosen mailbox anyway --
    # same reasoning as the pinned branch above: a revoked address is exactly
    # the fact the recruiter needs in order to fix it.
    return next((c for c in map(_owned, only) if c is not None), None)


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

    only = _rotation_subset(sequence)
    if only and not active_mailboxes(db, org_id=org_id, only=only):
        # The sequence names its mailboxes and every one of them is gone or
        # revoked. The rest of the pool is not a substitute -- naming a subset
        # is precisely the instruction to keep those other addresses off this
        # outreach -- so hold until one comes back.
        return ResolvedSender(
            FailingSender("this sequence's chosen mailboxes are not connected"),
            deferred=True,
            reason="mailbox_unavailable",
        )

    connection = pick_mailbox(db, org_id=org_id, now=now, only=only)
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

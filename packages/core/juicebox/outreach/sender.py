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

    @property
    def from_header(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""
    # Set by the Graph provider; reply detection matches on these.
    graph_message_id: str = ""
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


_graph_sender_factory = None


def set_graph_sender_factory(factory) -> None:
    """Tests inject a fake Graph sender; None restores the real one."""
    global _graph_sender_factory
    _graph_sender_factory = factory


def resolve_sender(db, sequence) -> EmailSender:
    """The provider for one sequence: its connected mailbox, else the global."""
    if not sequence.mailbox_id:
        return get_sender()

    from ..models import MailboxConnection

    connection = db.get(MailboxConnection, sequence.mailbox_id)
    if connection is None or connection.org_id != sequence.org_id:
        return FailingSender("mailbox connection not found")
    if connection.status != "active":
        return FailingSender(f"mailbox {connection.address} is {connection.status}; reconnect it")
    if _graph_sender_factory is not None:
        return _graph_sender_factory(db, connection)

    from ..integrations.microsoft import GraphSender

    return GraphSender(db, connection)

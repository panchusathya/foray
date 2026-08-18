"""Email delivery providers.

``console`` is the default so nothing leaves the machine during development;
``memory`` is what tests assert against; ``smtp`` is real delivery via a
mail server; ``graph`` (alias ``outlook``) sends through Microsoft Graph.
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
    org_id: str = ""

    @property
    def from_header(self) -> str:
        return f"{self.from_name} <{self.from_email}>" if self.from_name else self.from_email


@dataclass
class SendResult:
    ok: bool
    provider_message_id: str = ""
    error: str = ""


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
        elif settings.uses_graph_sender:
            from .graph import GraphSender

            _sender = GraphSender()
        elif provider == "memory":
            _sender = MemorySender()
        else:
            _sender = ConsoleSender()
    return _sender


def set_sender(sender: EmailSender | None) -> None:
    """Tests and the dry-run path swap the provider in directly."""
    global _sender
    _sender = sender

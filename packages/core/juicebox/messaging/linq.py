"""Linq Partner API adapter for messaging intake.

Inbound verification uses Standard Webhooks: HMAC-SHA256 over
``{webhook-id}.{webhook-timestamp}.{raw_body}``, with the signing secret
base64-encoded after the ``whsec_`` prefix. That differs from Retell's
``v=,d=`` over raw bytes, so it lives here rather than in ``intake.providers``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..models import IntakeNumber, IntakeThread

logger = logging.getLogger(__name__)

LINQ_API_BASE = "https://api.linqapp.com/api/partner/v3"
WEBHOOK_TOLERANCE_SECONDS = 300

Transport = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class SignatureError(RuntimeError):
    """Raised when a Linq webhook signature does not verify."""


class LinqSendError(RuntimeError):
    """Outbound delivery failed."""


@dataclass(frozen=True)
class InboundLinq:
    provider_native_id: str
    chat_id: str
    from_address: str
    to_address: str
    body: str
    is_group: bool
    raw: dict


def _decode_secret(secret: str) -> bytes:
    secret_str = secret.removeprefix("whsec_")
    return base64.b64decode(secret_str)


def _signed_content(*, webhook_id: str, timestamp: str, raw_body: bytes) -> str:
    return f"{webhook_id}.{timestamp}.{raw_body.decode('utf-8')}"


def _expected_signature(*, secret: str, signed_content: str) -> str:
    key = _decode_secret(secret)
    digest = hmac.new(key, signed_content.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_webhook(
    *,
    raw_body: bytes,
    webhook_id: str,
    timestamp: str,
    signature: str,
) -> None:
    """Raise ``SignatureError`` unless this request is authentic."""
    secret = get_settings().linq_webhook_secret
    if not secret:
        raise SignatureError("no Linq webhook secret configured; cannot verify webhooks")
    if not webhook_id or not timestamp or not signature:
        raise SignatureError("missing webhook-id, webhook-timestamp, or webhook-signature")

    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise SignatureError("malformed webhook-timestamp") from exc
    now = int(time.time())
    if abs(now - ts) > WEBHOOK_TOLERANCE_SECONDS:
        raise SignatureError("timestamp outside tolerance")

    signed = _signed_content(webhook_id=webhook_id, timestamp=timestamp, raw_body=raw_body)
    expected = _expected_signature(secret=secret, signed_content=signed)
    for part in signature.split():
        if part.startswith("v1,") and hmac.compare_digest(expected, part[3:]):
            return
    raise SignatureError("signature mismatch")


def sign_webhook(*, raw_body: bytes, webhook_id: str, timestamp: str, secret: str) -> str:
    """Produce a ``webhook-signature`` header value for replay tests."""
    signed = _signed_content(webhook_id=webhook_id, timestamp=timestamp, raw_body=raw_body)
    expected = _expected_signature(secret=secret, signed_content=signed)
    return f"v1,{expected}"


def _join_text_parts(parts: list) -> str:
    chunks: list[str] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        value = str(part.get("value") or "").strip()
        if value:
            chunks.append(value)
    return "\n".join(chunks)


def _handle_address(data: dict, *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            handle = value.get("handle")
            if isinstance(handle, str) and handle.strip():
                return handle.strip()
    return ""


def parse_inbound(payload: dict) -> InboundLinq:
    """Parse a ``message.received`` webhook envelope.

    Supports both ``2025-01-01`` (nested ``message``) and ``2026-02-03`` (flat
    ``data.id`` / ``data.parts``) shapes from the pinned subscription.
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("missing data object")

    is_group = bool(data.get("is_group"))
    chat = data.get("chat")
    if isinstance(chat, dict):
        is_group = is_group or bool(chat.get("is_group"))

    chat_id = str(data.get("chat_id") or (chat or {}).get("id") or "").strip()

    nested = data.get("message")
    if isinstance(nested, dict):
        provider_native_id = str(nested.get("id") or "").strip()
        parts = nested.get("parts") or []
        from_address = _handle_address(data, "from", "from_handle")
    else:
        provider_native_id = str(data.get("id") or "").strip()
        parts = data.get("parts") or []
        from_address = _handle_address(data, "sender_handle", "from", "from_handle")

    to_address = ""
    if isinstance(chat, dict):
        to_address = _handle_address(chat, "owner_handle", "from")
    if not to_address:
        to_address = _handle_address(data, "recipient_handle", "to")

    body = _join_text_parts(parts if isinstance(parts, list) else [])
    return InboundLinq(
        provider_native_id=provider_native_id,
        chat_id=chat_id,
        from_address=from_address,
        to_address=to_address,
        body=body,
        is_group=is_group,
        raw=data,
    )


def send_message(
    *,
    chat_id: str,
    body: str,
    idempotency_key: str,
    transport: Transport | None = None,
) -> str:
    """Send one outbound text reply. Returns Linq's message id."""
    settings = get_settings()
    if not settings.linq_api_key:
        raise LinqSendError("LINQ_API_KEY is not configured")
    if not chat_id:
        raise LinqSendError("chat_id is required")

    url = f"{LINQ_API_BASE}/chats/{chat_id}/messages"
    payload = {
        "message": {
            "parts": [{"type": "text", "value": body}],
            "idempotency_key": idempotency_key,
        }
    }
    try:
        if transport is not None:
            response = transport("POST", url, payload)
        else:
            http = httpx.post(
                url,
                headers={
                    "Authorization": f"Bearer {settings.linq_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            http.raise_for_status()
            response = http.json()
    except httpx.HTTPError as exc:
        logger.warning("Linq send failed: %s", exc)
        raise LinqSendError(str(exc)) from exc
    except ValueError as exc:
        raise LinqSendError(f"malformed response: {exc}") from exc

    message_id = ""
    if isinstance(response, dict):
        message_id = str(response.get("id") or (response.get("message") or {}).get("id") or "")
    message_id = message_id.strip()
    if not message_id:
        raise LinqSendError("Linq returned no message id")
    return message_id


def resolve_linq_org_id(db: Session, *, to_address: str) -> str | None:
    """Which org owns the Linq line that was texted. Never reads the payload."""
    number = lookup_key(to_address)
    if not number:
        return None
    row = db.scalar(
        select(IntakeNumber).where(
            IntakeNumber.phone == number,
            IntakeNumber.active.is_(True),
            IntakeNumber.provider == "linq",
        )
    )
    return row.org_id if row is not None else None


STATUS_EVENTS = frozenset(
    {"message.sent", "message.delivered", "message.read", "message.failed"}
)


@dataclass(frozen=True)
class StatusEvent:
    event_type: str
    message_id: str
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    failed_at: datetime | None = None
    error: str = ""
    raw: dict = field(default_factory=dict)


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_status_event(payload: dict) -> StatusEvent:
    """Parse a message.sent / delivered / read / failed envelope."""
    event_type = str(payload.get("event_type") or "").strip()
    if event_type not in STATUS_EVENTS:
        raise ValueError(f"unsupported status event {event_type!r}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("missing data object")
    nested = data.get("message") if isinstance(data.get("message"), dict) else data
    message_id = str(nested.get("id") or data.get("id") or "").strip()
    if not message_id:
        raise ValueError("no message id in status payload")
    error = ""
    err = nested.get("error") or data.get("error") or {}
    if isinstance(err, dict):
        error = str(err.get("reason") or err.get("code") or err.get("message") or "")
    elif err:
        error = str(err)
    return StatusEvent(
        event_type=event_type,
        message_id=message_id,
        delivered_at=parse_iso_datetime(nested.get("delivered_at") or data.get("delivered_at")),
        read_at=parse_iso_datetime(nested.get("read_at") or data.get("read_at")),
        failed_at=parse_iso_datetime(nested.get("failed_at") or data.get("failed_at")),
        error=error.strip(),
        raw=data,
    )


def start_typing(chat_id: str, *, transport: Transport | None = None) -> None:
    """Best-effort iMessage typing indicator. Failures are swallowed."""
    _typing_request("POST", chat_id, transport=transport)


def stop_typing(chat_id: str, *, transport: Transport | None = None) -> None:
    _typing_request("DELETE", chat_id, transport=transport)


def maybe_start_typing(thread: IntakeThread) -> None:
    """Show 'typing…' on Linq while a turn or scrape is in flight."""
    if (thread.channel or "") != "linq" or not thread.provider_thread_id:
        return
    try:
        start_typing(thread.provider_thread_id)
    except Exception as exc:  # noqa: BLE001 - never fail a turn on a typing blip
        logger.debug("linq typing indicator failed: %s", exc)


def maybe_stop_typing(thread: IntakeThread) -> None:
    """Clear 'typing…' when a turn ends without sending (held / already done)."""
    if (thread.channel or "") != "linq" or not thread.provider_thread_id:
        return
    try:
        stop_typing(thread.provider_thread_id)
    except Exception as exc:  # noqa: BLE001 - never fail a turn on a typing blip
        logger.debug("linq typing indicator stop failed: %s", exc)


def _typing_request(method: str, chat_id: str, *, transport: Transport | None = None) -> None:
    settings = get_settings()
    if not settings.linq_api_key or not chat_id:
        return
    url = f"{LINQ_API_BASE}/chats/{chat_id}/typing"
    try:
        if transport is not None:
            transport(method, url, {})
            return
        if method == "DELETE":
            httpx.delete(
                url,
                headers={"Authorization": f"Bearer {settings.linq_api_key}"},
                timeout=5.0,
            )
        else:
            httpx.post(
                url,
                headers={"Authorization": f"Bearer {settings.linq_api_key}"},
                timeout=5.0,
            )
    except httpx.HTTPError as exc:
        logger.debug("linq typing %s failed: %s", method, exc)

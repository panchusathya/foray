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


def start_chat(
    *,
    to_handle: str,
    body: str,
    idempotency_key: str,
    transport: Transport | None = None,
) -> tuple[str, str]:
    """Open a new chat with its first message. Returns (chat_id, message_id).

    ``POST /v3/chats`` — the outbound-initiate half of the Partner API. Used
    only by the voice handoff (``messaging/voice_handoff.py``), after an
    explicit opt-in captured on a recorded call. Everything else outbound
    stays a *reply* to an existing chat, which is what keeps the inbound-first
    posture checkable — this function is not a general-purpose sender, and a
    new caller of it needs the same consent story the handoff has.

    The parts shape mirrors ``send_message`` and ``parse_inbound`` (``value``,
    not ``body``) — one schema family across the integration, verified against
    the live reply path.
    """
    settings = get_settings()
    if not settings.linq_api_key:
        raise LinqSendError("LINQ_API_KEY is not configured")
    if not settings.linq_from_number:
        raise LinqSendError("LINQ_FROM_NUMBER is not configured")
    if not to_handle:
        raise LinqSendError("to_handle is required")

    url = f"{LINQ_API_BASE}/chats"
    payload = {
        "from": settings.linq_from_number,
        "to": [to_handle],
        "message": {
            "parts": [{"type": "text", "value": body}],
            "idempotency_key": idempotency_key,
        },
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
        logger.warning("Linq chat create failed: %s", exc)
        raise LinqSendError(str(exc)) from exc
    except ValueError as exc:
        raise LinqSendError(f"malformed response: {exc}") from exc

    chat_id = ""
    message_id = ""
    if isinstance(response, dict):
        chat = response.get("chat") if isinstance(response.get("chat"), dict) else {}
        chat_id = str(response.get("chat_id") or chat.get("id") or "").strip()
        message = response.get("message") if isinstance(response.get("message"), dict) else {}
        message_id = str(message.get("id") or response.get("message_id") or "").strip()
    if not chat_id:
        raise LinqSendError("Linq returned no chat id")
    return chat_id, message_id


def check_imessage(handle: str, *, transport: Transport | None = None) -> bool:
    """Whether this handle can receive iMessage. Fails closed.

    The Linq rollout is iMessage-only on purpose (``SMS_ENABLEMENT.md``):
    their SMS/RCS fallback rides a registration model that is still an open
    question, so a handle this check cannot vouch for is not texted — the
    caller parks the send for a human instead. An API error is therefore
    ``False``, never a retry loop: a recruiter following up beats a candidate
    silently not receiving a promised text.
    """
    settings = get_settings()
    if not settings.linq_api_key or not handle:
        return False
    url = f"{LINQ_API_BASE}/capability/check_imessage"
    payload = {"handle": handle}
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
                timeout=15.0,
            )
            http.raise_for_status()
            response = http.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Linq iMessage capability check failed for %s: %s", handle, exc)
        return False
    return bool(isinstance(response, dict) and response.get("capable"))


POLL_EVENTS = frozenset({"poll.sent", "poll.vote.added", "poll.vote.removed"})

# iMessage renders a poll natively, so picking a job is a tap rather than a
# sentence somebody has to phrase and we have to parse.
MAX_POLL_OPTIONS = 10
MAX_OPTION_CHARS = 100


def create_poll(
    *,
    chat_id: str,
    options: list[str],
    idempotency_key: str,
    transport: Transport | None = None,
) -> str:
    """Send a poll. Returns Linq's message id for the poll.

    The API takes no question -- the prompt is a separate text message, which
    is why the caller sends one first. Two options minimum; a poll with one
    job in it is a worse "apply?" button than just asking.
    """
    # Request shape first, configuration second: a call with one option is
    # wrong however the deployment is configured, and reporting a missing key
    # for it sends whoever is debugging to the wrong place.
    if not chat_id:
        raise LinqSendError("chat_id is required")
    trimmed = [o.strip()[:MAX_OPTION_CHARS] for o in options if o and o.strip()]
    if len(trimmed) < 2:
        raise LinqSendError("a poll needs at least two options")
    settings = get_settings()
    if not settings.linq_api_key:
        raise LinqSendError("LINQ_API_KEY is not configured")

    url = f"{LINQ_API_BASE}/chats/{chat_id}/polls"
    payload = {
        "poll": {
            "options": [{"text": text} for text in trimmed[:MAX_POLL_OPTIONS]],
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
        logger.warning("Linq poll send failed: %s", exc)
        raise LinqSendError(str(exc)) from exc
    except ValueError as exc:
        raise LinqSendError(f"malformed poll response: {exc}") from exc

    message_id = ""
    if isinstance(response, dict):
        message_id = str(
            response.get("id") or (response.get("message") or {}).get("id") or ""
        ).strip()
    if not message_id:
        raise LinqSendError("Linq returned no poll message id")
    return message_id


def parse_poll_options(payload: dict) -> tuple[str, list[tuple[str, str]]]:
    """``poll.sent`` -> (chat_id, [(option_id, text)]).

    This is the only place option ids are ever revealed: a vote event carries
    an option_id and no text, so without recording this mapping a tap cannot
    be resolved to a job.
    """
    data = payload.get("data") or payload
    chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or data.get("chat_id") or "")
    poll = data.get("poll") if isinstance(data.get("poll"), dict) else data
    options = []
    for option in poll.get("options") or []:
        if not isinstance(option, dict):
            continue
        option_id = str(option.get("option_id") or option.get("id") or "")
        text = str(option.get("text") or "")
        if option_id:
            options.append((option_id, text))
    return chat_id, options


def parse_poll_vote(payload: dict) -> tuple[str, str, bool]:
    """``poll.vote.added`` -> (chat_id, option_id, is_me).

    ``sender_handle.is_me`` is how a vote of ours is told from theirs -- these
    events carry no ``is_from_me``, unlike reactions.
    """
    data = payload.get("data") or payload
    handle = data.get("sender_handle") if isinstance(data.get("sender_handle"), dict) else {}
    chat = data.get("chat") if isinstance(data.get("chat"), dict) else {}
    return (
        str(chat.get("id") or data.get("chat_id") or ""),
        str(data.get("option_id") or ""),
        bool(handle.get("is_me")),
    )


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

# Tapbacks. Linq sends reaction.added / reaction.removed with a reaction_type
# of love | like | dislike | laugh | emphasize | question | custom | sticker.
REACTION_EVENTS = frozenset({"reaction.added", "reaction.removed"})
# The two a person means as "yes, this one". Deliberately not `emphasize` or
# `laugh`: acting on an ambiguous tapback would submit an application nobody
# asked for, and the cost of missing one is that they type "apply to 2".
POSITIVE_REACTIONS = frozenset({"like", "love"})


@dataclass(frozen=True)
class ReactionEvent:
    event_type: str
    chat_id: str
    message_id: str
    reaction_type: str
    is_from_me: bool

    @property
    def is_positive(self) -> bool:
        return (
            self.event_type == "reaction.added"
            and not self.is_from_me
            and self.reaction_type in POSITIVE_REACTIONS
        )


def parse_reaction(payload: dict) -> ReactionEvent:
    """A tapback envelope, flattened. Raises ValueError when unusable."""
    event_type = str(payload.get("event_type") or "")
    if event_type not in REACTION_EVENTS:
        raise ValueError(f"not a reaction event: {event_type!r}")
    data = payload.get("data") or payload
    nested = data.get("reaction") if isinstance(data.get("reaction"), dict) else data
    chat_id = str(nested.get("chat_id") or data.get("chat_id") or "")
    message_id = str(nested.get("message_id") or data.get("message_id") or "")
    if not chat_id or not message_id:
        raise ValueError("reaction event has no chat_id/message_id")
    return ReactionEvent(
        event_type=event_type,
        chat_id=chat_id,
        message_id=message_id,
        reaction_type=str(nested.get("reaction_type") or "").lower(),
        is_from_me=bool(nested.get("is_from_me")),
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


def mark_chat_read(chat_id: str, *, transport: Transport | None = None) -> None:
    """Send a read receipt for the whole chat.

    Chat-level is all Linq exposes -- there is no per-message read endpoint --
    which suits us: we read a thread, not individual bubbles.

    Paired with the typing indicator on purpose. Read, then typing, then the
    reply is the sequence a person produces, and without the first one the
    candidate watches "Delivered" sit there while we think.
    """
    _chat_action("POST", chat_id, "read", transport=transport)


def maybe_mark_read(thread: IntakeThread) -> None:
    """Best-effort read receipt for a Linq thread we are about to answer."""
    if (thread.channel or "") != "linq" or not thread.provider_thread_id:
        return
    try:
        mark_chat_read(thread.provider_thread_id)
    except Exception as exc:  # noqa: BLE001 - never fail a turn over a receipt
        logger.debug("linq read receipt failed: %s", exc)


def maybe_start_typing(thread: IntakeThread) -> None:
    """Show 'typing…' on Linq while a turn or scrape is in flight."""
    if (thread.channel or "") != "linq" or not thread.provider_thread_id:
        return
    try:
        start_typing(thread.provider_thread_id)
    except Exception as exc:  # noqa: BLE001 - never fail a turn on a typing blip
        logger.debug("linq typing indicator failed: %s", exc)


def _typing_request(method: str, chat_id: str, *, transport: Transport | None = None) -> None:
    _chat_action(method, chat_id, "typing", transport=transport)


def _chat_action(
    method: str, chat_id: str, action: str, *, transport: Transport | None = None
) -> None:
    """POST/DELETE a side-effect on a chat: typing, read. Never raises.

    These are courtesies. A failed one costs an indicator, and a turn that
    died because a receipt 500'd would cost the conversation.
    """
    settings = get_settings()
    # An injected transport IS the client, so it does not need our API key --
    # requiring one here made these unreachable in tests, which is how a
    # courtesy call ends up shipped and never exercised.
    if not chat_id or (transport is None and not settings.linq_api_key):
        return
    url = f"{LINQ_API_BASE}/chats/{chat_id}/{action}"
    try:
        if transport is not None:
            transport(method, url, {})
            return
        headers = {"Authorization": f"Bearer {settings.linq_api_key}"}
        if method == "DELETE":
            response = httpx.delete(url, headers=headers, timeout=5.0)
        else:
            response = httpx.post(url, headers=headers, timeout=5.0)
        # httpx does not raise on 4xx/5xx, so without this a wrong endpoint or
        # a plan that does not include typing indicators looks identical to
        # success and the dots never appear with nothing in the logs to say
        # why. Still best-effort -- we log and move on, never fail a turn.
        if response.status_code >= 400:
            logger.warning(
                "linq typing %s -> %s (%s)",
                method,
                response.status_code,
                (response.text or "")[:200],
            )
    except httpx.HTTPError as exc:
        logger.warning("linq %s %s failed: %s", action, method, exc)

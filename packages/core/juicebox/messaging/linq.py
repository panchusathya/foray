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
from collections.abc import Callable, Sequence
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


# What Linq says it actually delivered over. Canonical spellings, because the
# payload's casing is not guaranteed and this string is stored and compared.
SERVICE_IMESSAGE = "iMessage"
SERVICE_RCS = "RCS"
SERVICE_SMS = "SMS"
# The two that render an inline image without turning it into an MMS.
RICH_SERVICES = frozenset({SERVICE_IMESSAGE, SERVICE_RCS})

_SERVICES = {
    "imessage": SERVICE_IMESSAGE,
    "rcs": SERVICE_RCS,
    "sms": SERVICE_SMS,
    "mms": SERVICE_SMS,
}

# The formats Linq accepts for a voice memo, which is also the set worth
# transcribing. A content type absent from here is refused with an explanation
# rather than handed to a provider that will reject it after the download.
AUDIO_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/x-caf": ".caf",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
    "audio/amr": ".amr",
}

def normalize_service(value: Any) -> str:
    """``"imessage"`` -> ``"iMessage"``; anything unrecognised -> ``""``.

    Empty is meaningful and must not become a default: "we have never seen a
    protocol on this thread" is the case that has to fall back to text, and a
    guess of ``SMS`` or ``iMessage`` would each be wrong half the time.
    """
    return _SERVICES.get(str(value or "").strip().lower(), "")


@dataclass(frozen=True)
class InboundAttachment:
    """One media part of an inbound message: a file the sender attached.

    ``url`` points at Linq's CDN. On the persistent tier it is long-lived; on
    the ephemeral tier it is a signed URL with a ~15-minute TTL, refreshable
    through ``GET /v3/attachments/{attachment_id}`` — which is why the id is
    carried alongside it.
    """

    url: str
    content_type: str = ""
    filename: str = ""
    size: int = 0
    attachment_id: str = ""

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "content_type": self.content_type,
            "filename": self.filename,
            "size": self.size,
            "attachment_id": self.attachment_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> InboundAttachment:
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(
            url=str(data.get("url") or ""),
            content_type=str(data.get("content_type") or ""),
            filename=str(data.get("filename") or ""),
            size=size,
            attachment_id=str(data.get("attachment_id") or ""),
        )


@dataclass(frozen=True)
class InboundLinq:
    provider_native_id: str
    chat_id: str
    from_address: str
    to_address: str
    body: str
    is_group: bool
    raw: dict
    attachments: tuple[InboundAttachment, ...] = ()
    # iMessage | RCS | SMS, or empty when the payload did not say.
    service: str = ""

    @property
    def audio(self) -> InboundAttachment | None:
        """The first audio attachment, which is what a voice memo arrives as."""
        return next((a for a in self.attachments if is_audio(a.content_type)), None)


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


def _media_parts(parts: list) -> tuple[InboundAttachment, ...]:
    """The files attached to a message, in the order they were sent.

    Key names are read defensively (``url``/``value``, ``filename``/``name``)
    for the same reason ``_handle_address`` is: two envelope vintages are
    pinned, and a rename in a third must degrade to a missing field, not a
    lost file.
    """
    found: list[InboundAttachment] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("type") not in ("media", "attachment", "file"):
            continue
        url = str(part.get("url") or part.get("value") or "").strip()
        if not url:
            continue
        try:
            size = int(part.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        found.append(
            InboundAttachment(
                url=url,
                content_type=str(
                    part.get("content_type") or part.get("mime_type") or ""
                ).strip(),
                filename=str(part.get("filename") or part.get("name") or "").strip(),
                size=size,
                attachment_id=str(
                    part.get("attachment_id") or part.get("id") or ""
                ).strip(),
            )
        )
    return tuple(found)


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
        service = normalize_service(nested.get("service") or data.get("service"))
    else:
        provider_native_id = str(data.get("id") or "").strip()
        parts = data.get("parts") or []
        from_address = _handle_address(data, "sender_handle", "from", "from_handle")
        service = normalize_service(data.get("service"))

    to_address = ""
    if isinstance(chat, dict):
        to_address = _handle_address(chat, "owner_handle", "from")
    if not to_address:
        to_address = _handle_address(data, "recipient_handle", "to")

    part_list = parts if isinstance(parts, list) else []
    return InboundLinq(
        provider_native_id=provider_native_id,
        chat_id=chat_id,
        from_address=from_address,
        to_address=to_address,
        body=_join_text_parts(part_list),
        is_group=is_group,
        raw=data,
        attachments=_media_parts(part_list),
        service=service,
    )


@dataclass(frozen=True)
class OutboundMedia:
    """A file we are asking Linq to attach to a message we are sending.

    Deliberately not ``InboundAttachment``: that describes a file somebody sent
    us and always has a URL, while this carries either a pre-uploaded
    ``attachment_id`` or a public ``url``. One type for both directions is how
    a field that is required one way quietly becomes optional the other.
    """

    content_type: str
    attachment_id: str = ""
    url: str = ""
    filename: str = ""


def is_audio(content_type: str) -> bool:
    """Whether this is a voice memo we can transcribe."""
    return (content_type or "").strip().lower() in AUDIO_SUFFIXES


def audio_suffix(content_type: str) -> str:
    return AUDIO_SUFFIXES.get((content_type or "").strip().lower(), ".m4a")


def _outbound_parts(body: str, media: Sequence[OutboundMedia]) -> list[dict[str, Any]]:
    """Media first, then the text.

    Linq forbids *consecutive text* parts, so one media part plus one text part
    is fine and two text parts are not -- which is why the body is never split.
    Media leads because that is the order a phone renders it in: the card, then
    the sentence about the card.
    """
    parts: list[dict[str, Any]] = []
    for part in media or ():
        if part.attachment_id:
            parts.append({"type": "media", "attachment_id": part.attachment_id})
        elif part.url:
            parts.append({"type": "media", "url": part.url})
    if body:
        parts.append({"type": "text", "value": body})
    return parts


def send_message(
    *,
    chat_id: str,
    body: str,
    idempotency_key: str,
    media: Sequence[OutboundMedia] = (),
    transport: Transport | None = None,
) -> str:
    """Send one outbound reply, optionally carrying media. Returns Linq's id."""
    settings = get_settings()
    if not settings.linq_api_key:
        raise LinqSendError("LINQ_API_KEY is not configured")
    if not chat_id:
        raise LinqSendError("chat_id is required")

    parts = _outbound_parts(body, media)
    if not parts:
        raise LinqSendError("a message needs a body or a media part")

    url = f"{LINQ_API_BASE}/chats/{chat_id}/messages"
    payload = {
        "message": {
            "parts": parts,
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


#: ``POST /attachments`` answers with a presigned PUT valid for 15 minutes.
#: Nothing here holds one that long -- the upload follows immediately -- but the
#: window is why the id is what gets cached and the URL never is.
ATTACHMENT_UPLOAD_TIMEOUT = 60.0


def upload_attachment(
    *,
    filename: str,
    content_type: str,
    data: bytes,
    transport: Transport | None = None,
    uploader: Callable[[str, dict[str, str], bytes], None] | None = None,
) -> str:
    """Pre-upload bytes to Linq and return the durable ``attachment_id``.

    Two legs: ask for a presigned URL, then PUT to it. Returns the id, which
    does not expire and is what a later send references.

    **This is the path an outbound image takes, not a public URL of our own.**
    There is no object store or CDN here and no static mount; the only
    unauthenticated file route (``routers/portal.py``) signs for sixty seconds,
    which is nowhere near enough for a vendor-side fetch. Handing Linq the
    bytes sidesteps the whole question.
    """
    settings = get_settings()
    if not settings.linq_api_key and transport is None:
        raise LinqSendError("LINQ_API_KEY is not configured")
    if not data:
        raise LinqSendError("nothing to upload")

    url = f"{LINQ_API_BASE}/attachments"
    payload = {
        "filename": filename or "attachment",
        "content_type": content_type,
        "size_bytes": len(data),
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
                timeout=ATTACHMENT_UPLOAD_TIMEOUT,
            )
            http.raise_for_status()
            response = http.json()
    except httpx.HTTPError as exc:
        logger.warning("Linq attachment reservation failed: %s", exc)
        raise LinqSendError(str(exc)) from exc
    except ValueError as exc:
        raise LinqSendError(f"malformed attachment response: {exc}") from exc

    if not isinstance(response, dict):
        raise LinqSendError("malformed attachment response")
    attachment_id = str(response.get("attachment_id") or response.get("id") or "").strip()
    upload_url = str(response.get("upload_url") or "").strip()
    headers = response.get("required_headers")
    headers = {str(k): str(v) for k, v in headers.items()} if isinstance(headers, dict) else {}
    if not attachment_id or not upload_url:
        raise LinqSendError("Linq returned no attachment_id/upload_url")

    if uploader is not None:
        uploader(upload_url, headers, data)
        return attachment_id

    headers.setdefault("Content-Type", content_type)
    try:
        put = httpx.put(
            upload_url, headers=headers, content=data, timeout=ATTACHMENT_UPLOAD_TIMEOUT
        )
        put.raise_for_status()
    except httpx.HTTPError as exc:
        # The id exists but has no bytes behind it, so it must not be cached --
        # a send referencing it would fail forever. Raising is what stops the
        # caller storing it.
        logger.warning("Linq attachment upload failed: %s", exc)
        raise LinqSendError(f"attachment upload failed: {exc}") from exc
    return attachment_id


class MediaFetchError(RuntimeError):
    """Downloading an attachment failed, after any refresh retry."""


#: Test seam: ``url -> (status_code, content)``. Mirrors ``Transport`` for
#: JSON calls; media needs bytes, so it is its own callable.
MediaTransport = Callable[[str], tuple[int, bytes]]


def fetch_media(
    url: str,
    *,
    attachment_id: str = "",
    transport: MediaTransport | None = None,
) -> bytes:
    """Download one attachment, capped at the upload limit.

    A failed GET is retried once through ``GET /v3/attachments/{id}`` when the
    id is known — the ephemeral tier signs URLs with a short TTL, and a worker
    that picks the job up late holds an expired link, not a lost file.
    """
    if not url:
        raise MediaFetchError("no attachment url")
    status, content = _media_get(url, transport=transport)
    if status >= 400 and attachment_id:
        fresh = _refresh_media_url(attachment_id)
        if fresh and fresh != url:
            status, content = _media_get(fresh, transport=transport)
    if status >= 400:
        raise MediaFetchError(f"attachment download returned {status}")
    cap = get_settings().max_upload_bytes
    if len(content) > cap:
        raise MediaFetchError(f"attachment exceeds the {cap // (1024 * 1024)} MB limit")
    return content


def _media_get(url: str, *, transport: MediaTransport | None = None) -> tuple[int, bytes]:
    if transport is not None:
        return transport(url)
    try:
        response = httpx.get(url, timeout=30.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise MediaFetchError(str(exc)) from exc
    return response.status_code, response.content


def _refresh_media_url(attachment_id: str) -> str:
    """A fresh signed URL for an expired one. Empty string when unavailable."""
    settings = get_settings()
    if not settings.linq_api_key:
        return ""
    try:
        response = httpx.get(
            f"{LINQ_API_BASE}/attachments/{attachment_id}",
            headers={"Authorization": f"Bearer {settings.linq_api_key}"},
            timeout=15.0,
        )
        if response.status_code >= 400:
            return ""
        data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("attachment url refresh failed for %s: %s", attachment_id, exc)
        return ""
    if not isinstance(data, dict):
        return ""
    nested = data.get("attachment") if isinstance(data.get("attachment"), dict) else data
    inner = nested.get("data") if isinstance(nested.get("data"), dict) else nested
    return str(inner.get("url") or "").strip()


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
    # What Linq actually delivered over. On `message.sent` this is the most
    # reliable protocol signal there is -- it is the outcome, not a capability
    # guess -- which is what decides whether the next card carries an image.
    service: str = ""
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
        service=normalize_service(nested.get("service") or data.get("service")),
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

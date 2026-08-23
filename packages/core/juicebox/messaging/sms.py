"""Twilio SMS adapter for messaging intake.

Inbound verification uses Twilio's request signature: HMAC-SHA1 over the full
URL plus sorted POST parameter names and values. That is a different scheme
from Retell's ``v=,d=`` over raw bytes, so it lives here rather than in
``intake.providers``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..models import IntakeNumber

logger = logging.getLogger(__name__)

TWILIO_API_BASE = "https://api.twilio.com/2010-04-01"


class SignatureError(RuntimeError):
    """Raised when a Twilio webhook signature does not verify."""


@dataclass(frozen=True)
class InboundSms:
    provider_native_id: str
    from_address: str
    to_address: str
    body: str
    raw: dict[str, str]


def _signature_base(url: str, params: dict[str, str]) -> str:
    pieces = [url]
    for key in sorted(params):
        pieces.append(key)
        pieces.append(params[key])
    return "".join(pieces)


def verify_request(*, url: str, params: dict[str, str], signature: str) -> None:
    """Raise ``SignatureError`` unless this request is authentic."""
    auth_token = get_settings().twilio_auth_token
    if not auth_token:
        raise SignatureError("no Twilio auth token configured; cannot verify webhooks")
    if not signature:
        raise SignatureError("missing X-Twilio-Signature")

    expected = base64.b64encode(
        hmac.new(
            auth_token.encode("utf-8"),
            _signature_base(url, params).encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")
    if not hmac.compare_digest(expected, signature):
        raise SignatureError("signature mismatch")


def sign_request(*, url: str, params: dict[str, str], auth_token: str) -> str:
    """Produce a header for replay tests."""
    return base64.b64encode(
        hmac.new(
            auth_token.encode("utf-8"),
            _signature_base(url, params).encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("ascii")


def parse_inbound(params: dict[str, str]) -> InboundSms:
    return InboundSms(
        provider_native_id=(params.get("MessageSid") or "").strip(),
        from_address=(params.get("From") or "").strip(),
        to_address=(params.get("To") or "").strip(),
        body=params.get("Body") or "",
        raw=dict(params),
    )


def send_sms(*, to_address: str, body: str) -> str:
    """Send one outbound SMS. Returns Twilio's message sid."""
    settings = get_settings()
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        raise RuntimeError("Twilio credentials are not configured")
    from_number = settings.sms_from_number
    if not from_number:
        raise RuntimeError("SMS_FROM_NUMBER is not configured")

    url = f"{TWILIO_API_BASE}/Accounts/{settings.twilio_account_sid}/Messages.json"
    response = httpx.post(
        url,
        auth=(settings.twilio_account_sid, settings.twilio_auth_token),
        data={"From": from_number, "To": to_address, "Body": body},
        timeout=30.0,
    )
    if response.status_code >= 400:
        detail = response.text[:400]
        logger.warning("Twilio send failed (%s): %s", response.status_code, detail)
        raise RuntimeError(detail or f"Twilio HTTP {response.status_code}")

    payload: dict[str, Any] = response.json()
    message_sid = str(payload.get("sid") or "").strip()
    if not message_sid:
        raise RuntimeError("Twilio returned no message sid")
    return message_sid


def form_body(params: dict[str, str]) -> bytes:
    return urlencode(params).encode("utf-8")


def resolve_sms_org_id(db: Session, *, to_address: str) -> str | None:
    """Which org owns the number that was texted. Never reads the payload."""
    number = lookup_key(to_address)
    if not number:
        return None
    row = db.scalar(
        select(IntakeNumber).where(
            IntakeNumber.phone == number,
            IntakeNumber.active.is_(True),
            IntakeNumber.sms_enabled.is_(True),
        )
    )
    return row.org_id if row is not None else None

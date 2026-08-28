"""The narrow, signed boundary between JuiceBox and OpenInstinct.

OpenInstinct is deliberately a separate deployment and database.  This module
only signs short-lived service requests and reads recruiter-visible
conversation data; it never reads a vault secret or browser session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import get_settings

ISSUER = "goforay-openinstinct"
JUICEBOX_AUDIENCE = "juicebox"
OPENINSTINCT_AUDIENCE = "openinstinct"


class OpenInstinctError(RuntimeError):
    """The remote agent is unavailable or its bridge credentials are invalid."""


@dataclass(frozen=True)
class OpenInstinctPrincipal:
    subject: str
    identities: tuple[dict[str, str], ...]


def _secret() -> str:
    secret = get_settings().openinstinct_shared_secret
    if not secret:
        raise OpenInstinctError("OpenInstinct integration is not configured")
    return secret


def _b64e(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _signature(signing_input: bytes) -> str:
    return _b64e(hmac.new(_secret().encode(), signing_input, hashlib.sha256).digest())


def mint_bridge_token(
    *,
    audience: str,
    subject: str,
    org_id: str = "",
    candidate_id: str = "",
    identities: list[dict[str, str]] | None = None,
    ttl_seconds: int = 300,
) -> str:
    """Make a five-minute service token usable only by the chosen audience."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": ISSUER,
        "aud": audience,
        "sub": subject,
        "iat": now,
        "exp": now + ttl_seconds,
    }
    if org_id:
        payload["org"] = org_id
    if candidate_id:
        payload["candidate"] = candidate_id
    if identities:
        payload["identities"] = identities
    header = _b64e(b'{"alg":"HS256","typ":"JWT"}')
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    signed = f"{header}.{body}"
    return f"{signed}.{_signature(signed.encode())}"


def verify_bridge_token(token: str, *, audience: str) -> OpenInstinctPrincipal:
    """Verify an OpenInstinct request without accepting a JuiceBox user token."""
    try:
        header, body, signature = token.split(".")
        signed = f"{header}.{body}".encode()
        if not hmac.compare_digest(_signature(signed), signature):
            raise ValueError("signature")
        payload = json.loads(_b64d(body))
    except Exception as exc:
        raise OpenInstinctError("invalid OpenInstinct service token") from exc
    if (
        payload.get("iss") != ISSUER
        or payload.get("aud") != audience
        or not isinstance(payload.get("sub"), str)
        or int(payload.get("exp", 0)) < int(time.time())
    ):
        raise OpenInstinctError("expired or mis-scoped OpenInstinct service token")
    raw_identities = payload.get("identities") or []
    identities = tuple(
        {"kind": str(item.get("kind") or ""), "value": str(item.get("value") or "")}
        for item in raw_identities
        if isinstance(item, dict) and item.get("verified") is True
    )
    return OpenInstinctPrincipal(subject=payload["sub"], identities=identities)


def fetch_candidate_conversations(*, org_id: str, candidate_id: str) -> dict[str, Any]:
    """Read OpenInstinct's timeline for a recruiter, never write or mirror it."""
    settings = get_settings()
    if not settings.openinstinct_url or not settings.openinstinct_shared_secret:
        return {"conversations": [], "available": False}
    token = mint_bridge_token(
        audience=OPENINSTINCT_AUDIENCE,
        subject="juicebox-recruiter-workspace",
        org_id=org_id,
        candidate_id=candidate_id,
    )
    try:
        response = httpx.get(
            f"{settings.openinstinct_url}/api/goforay/conversations",
            params={"candidate_id": candidate_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=settings.openinstinct_request_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        return {"conversations": [], "available": False}
    rows = payload.get("conversations") if isinstance(payload, dict) else None
    return {"conversations": rows if isinstance(rows, list) else [], "available": True}

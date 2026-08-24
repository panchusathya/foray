"""Verify a Google ID token, so a candidate can sign in without email.

Why this exists: outbound SMTP is blocked at the network level on the prod
VM (587, 465, 25 and 2525 all time out, to Zoho and to Gmail alike, while
general HTTPS is fine). That makes the OTP door undeliverable no matter what
credentials are configured. A sign-in that sends no email cannot be blocked
by a blocked mail port, and most candidates already have a Google account.

**Validation is delegated to Google's tokeninfo endpoint** rather than done
locally against their JWKS. Local RS256 verification is the higher-volume
answer and Google recommend it at scale, but it means hand-rolling signature
checking and key-rotation handling next to an authentication boundary -- the
worst place in the system to have a subtle bug. One HTTPS call per sign-in is
an easy price for not writing that code. If volume ever makes it matter, this
module is the only thing that changes.

The claims are then checked here, not assumed: Google says the token is
well-formed and unexpired, and we say whether it was issued for *us* and
whether the address is actually proven.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class GoogleAuthError(RuntimeError):
    """The token is not one we can act on. The message is for logs, not users."""


@dataclass(frozen=True)
class GoogleIdentity:
    email: str
    name: str
    subject: str


def verify_id_token(credential: str) -> GoogleIdentity:
    settings = get_settings()
    if not settings.google_client_id:
        raise GoogleAuthError("google sign-in is not configured")
    if not credential:
        raise GoogleAuthError("no credential supplied")

    try:
        response = httpx.get(
            TOKENINFO_URL, params={"id_token": credential}, timeout=10.0
        )
    except httpx.HTTPError as exc:
        raise GoogleAuthError(f"tokeninfo unreachable: {exc}") from exc
    if response.status_code != 200:
        raise GoogleAuthError(f"tokeninfo rejected the token ({response.status_code})")
    try:
        claims = response.json()
    except ValueError as exc:
        raise GoogleAuthError("tokeninfo returned non-JSON") from exc

    # Audience is the whole point of this check: a valid Google token issued
    # for somebody else's app is still a valid Google token. Without this,
    # anyone with any Google client could mint sessions here.
    if claims.get("aud") != settings.google_client_id:
        raise GoogleAuthError("token was issued for a different application")
    if str(claims.get("iss") or "") not in _ISSUERS:
        raise GoogleAuthError(f"unexpected issuer {claims.get('iss')!r}")

    # Google returns this as the string "true"/"false" on this endpoint.
    verified = str(claims.get("email_verified") or "").lower() in {"true", "1"}
    email = str(claims.get("email") or "").strip().lower()
    if not email or not verified:
        # An unverified address proves nothing about who is holding it, and
        # matching it to a candidate record would hand over their profile.
        raise GoogleAuthError("email missing or not verified by google")

    return GoogleIdentity(
        email=email,
        name=str(claims.get("name") or "").strip(),
        subject=str(claims.get("sub") or ""),
    )

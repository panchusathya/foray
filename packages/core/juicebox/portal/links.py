"""Minting portal magic links — the single door.

The thread agent, the notify sender, and the recruiter button all call
``mint_portal_link``; nothing else builds a portal URL, so the single-use
grant and the TTL cannot be bypassed by one caller forgetting them.

The link is ``{portal_base_url}/m/<code>`` — one short opaque segment. It
used to be a full JWT in that position, which made every texted link several
hundred characters of bearer credential: unreadable at the end of a message,
and copied into every screenshot, preview fetch and browser history along the
way. The grant row was already what made the link single-use, so the signature
was doing no work the row was not; ``link_code`` is 80 bits from ``secrets``
and the row does the rest.

The landing path travels on the row too (``next_path``), for the same reason
it used to travel inside the token rather than as a query parameter: nothing
about where somebody is going belongs in a URL that gets forwarded.

Opening the link is a GET of the SPA with no side effects; the exchange is a
POST from its JavaScript, so an iMessage preview prefetch cannot consume the
grant.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CandidateAuthGrant, utcnow
from ..security import create_access_token

#: 16 base32 characters, 80 bits. Long enough that guessing a live single-use
#: code is not a strategy, short enough to read out loud.
LINK_CODE_BYTES = 10


def new_link_code() -> str:
    """An unguessable URL segment. `token_hex` rather than `token_urlsafe`:
    base64url is case-sensitive and mixed case in a texted link invites
    transcription errors, so the alphabet stays [0-9a-f]."""
    return secrets.token_hex(LINK_CODE_BYTES)


def mint_portal_link(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    path: str = "/",
    channel: str = "sms",
    ttl_seconds: int | None = None,
) -> str:
    """A single-use, expiring deep link into the portal for one candidate."""
    settings = get_settings()
    ttl = ttl_seconds or settings.portal_link_ttl_seconds
    grant = CandidateAuthGrant(
        org_id=org_id,
        candidate_id=candidate_id,
        channel=channel,
        link_code=new_link_code(),
        next_path=safe_next(path),
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    db.add(grant)
    db.flush()
    base = settings.portal_base_url.rstrip("/")
    return f"{base}/m/{grant.link_code}"


def safe_next(path: str) -> str:
    """A same-origin path, or ``/``.

    The exchange hands this straight to the SPA router, so an absolute URL
    would be an open redirect on a link we text to people -- exactly the shape
    a phishing hop wants. Protocol-relative (``//host``) is refused for the
    same reason: browsers treat it as absolute.
    """
    candidate = (path or "/").strip()
    if not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    return candidate[:400]


def mint_session_token(*, org_id: str, candidate_id: str) -> str:
    """The week-long session a link or an OTP exchanges into."""
    settings = get_settings()
    return create_access_token(
        subject=candidate_id,
        org_id=org_id,
        role="candidate",
        ttl_seconds=settings.portal_session_ttl_seconds,
        extra={"typ": "candidate"},
    )

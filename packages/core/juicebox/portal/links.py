"""Minting portal magic links — the single door.

The thread agent, the notify sender, and the recruiter button all call
``mint_portal_link``; nothing else builds a portal URL, so the single-use
grant and the TTL cannot be bypassed by one caller forgetting them.

The link is ``{portal_base_url}/m/<jwt>`` — one opaque segment, the Tsenta
shape, friendly to SMS previews. The path the user should land on after the
exchange travels INSIDE the token (``next`` claim), so the URL never grows a
second query parameter for anything sensitive. Opening the link is a GET of
the SPA with no side effects; the exchange is a POST from its JavaScript, so
an iMessage preview prefetch cannot consume the grant.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import CandidateAuthGrant, utcnow
from ..security import create_access_token


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
        expires_at=utcnow() + timedelta(seconds=ttl),
    )
    db.add(grant)
    db.flush()
    token = create_access_token(
        subject=candidate_id,
        org_id=org_id,
        role="candidate",
        ttl_seconds=ttl,
        extra={"typ": "portal-link", "jti": grant.id, "next": path or "/"},
    )
    base = settings.portal_base_url.rstrip("/")
    return f"{base}/m/{token}"


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

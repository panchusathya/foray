"""Portal login: magic-link exchange and the email OTP return path.

All three endpoints are public — the person on the other end has no session
yet, which is the point. The protections are structural rather than
credential-shaped: the link token is signed, expiring, and single-use via its
grant row; the OTP is hashed at rest, attempt-capped, issuance-capped, and
the request endpoint always answers 202 so an attacker cannot enumerate which
addresses exist.

Multi-org email collisions are refused, not guessed: if one address maps to
candidates in more than one org, no code is sent — the org-scoped magic link
the agent texts is the login for that person. Guessing "most recently
updated" would sign somebody into the wrong tenant the day a second org
onboards.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import CommittingRoute, get_db
from ..models import Candidate, CandidateAuthGrant, CandidateEmail, PortalOtp, utcnow
from ..outreach.sender import OutboundEmail, sender_for_org
from ..portal.links import mint_session_token
from ..portal.mail import portal_sender
from ..security import TokenError, decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/portal/auth", tags=["portal"])

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired link"
)


class ExchangeIn(BaseModel):
    token: str


class OtpRequestIn(BaseModel):
    email: str


class OtpVerifyIn(BaseModel):
    email: str
    code: str


def _aware(value):

    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@router.post("/exchange")
def exchange(payload: ExchangeIn, db: Session = Depends(get_db)) -> dict:
    """One magic link → one session. The grant row makes it one."""
    try:
        claims = decode_access_token(payload.token)
    except TokenError as exc:
        raise _UNAUTHORIZED from exc
    if claims.get("typ") != "portal-link":
        raise _UNAUTHORIZED

    grant = db.get(CandidateAuthGrant, str(claims.get("jti") or ""))
    now = utcnow()
    if (
        grant is None
        or grant.candidate_id != claims.get("sub")
        or grant.consumed_at is not None
        or _aware(grant.expires_at) < now
    ):
        raise _UNAUTHORIZED
    candidate = db.get(Candidate, grant.candidate_id)
    if candidate is None or candidate.org_id != grant.org_id:
        raise _UNAUTHORIZED

    grant.consumed_at = now
    db.flush()
    settings = get_settings()
    return {
        "access_token": mint_session_token(
            org_id=candidate.org_id, candidate_id=candidate.id
        ),
        "expires_in": settings.portal_session_ttl_seconds,
        "next": str(claims.get("next") or "/"),
        "candidate": {"first_name": candidate.first_name or candidate.full_name or ""},
    }


def _hash_code(code: str) -> str:
    secret = get_settings().secret_key
    return hmac.new(secret.encode(), f"portal-otp:{code}".encode(), hashlib.sha256).hexdigest()


def _candidates_for_email(db: Session, email: str) -> list[Candidate]:
    rows = db.scalars(
        select(CandidateEmail).where(CandidateEmail.email == email)
    ).all()
    candidates = []
    for row in rows:
        candidate = db.get(Candidate, row.candidate_id)
        if candidate is not None:
            candidates.append(candidate)
    return candidates


class GoogleIn(BaseModel):
    credential: str = ""


@router.get("/config")
def auth_config() -> dict:
    """What sign-in doors this deployment actually has open.

    The portal asks rather than hardcoding, so a deployment with no Google
    client configured does not render a button that cannot work.
    """
    return {"google_client_id": get_settings().google_client_id}


@router.post("/google")
def google_sign_in(payload: GoogleIn, db: Session = Depends(get_db)) -> dict:
    """Exchange a Google ID token for a portal session.

    Deliberately NOT always-202 like the OTP door. There the vagueness
    protects a stranger's privacy: a 202 for every address stops the endpoint
    confirming who is a candidate. Here the caller has already proven they own
    the address, so telling them we have no record is telling them about
    themselves, and a silent failure would leave them staring at a sign-in
    button that does nothing.
    """
    from ..portal.google import GoogleAuthError, verify_id_token

    try:
        identity = verify_id_token(payload.credential)
    except GoogleAuthError as exc:
        logger.info("google sign-in refused: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google sign-in failed"
        ) from exc

    candidates = _candidates_for_email(db, identity.email)
    if not candidates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No application found for that address.",
        )
    orgs = {candidate.org_id for candidate in candidates}
    if len(orgs) > 1:
        # Same rule as the OTP path: refuse rather than guess which tenant.
        # The org-scoped magic link remains the way in for these people.
        logger.info("google sign-in refused: %s maps to %d orgs", identity.email, len(orgs))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That address is registered with more than one team — use the link we texted you.",
        )

    candidate = candidates[0]
    return {
        "access_token": mint_session_token(
            org_id=candidate.org_id, candidate_id=candidate.id
        ),
        "expires_in": get_settings().portal_session_ttl_seconds,
    }


@router.post("/otp/request", status_code=202)
def otp_request(payload: OtpRequestIn, db: Session = Depends(get_db)) -> dict:
    """Always 202. Whether a code went out is not something this endpoint
    reveals — an unknown address, a capped address, and a sent code all read
    identically from outside."""
    email = (payload.email or "").strip().lower()
    accepted = {"status": "accepted"}
    if not email:
        return accepted

    candidates = _candidates_for_email(db, email)
    if not candidates:
        return accepted
    orgs = {candidate.org_id for candidate in candidates}
    if len(orgs) > 1:
        # Refused, not guessed. The magic link the agent texts is org-scoped
        # and remains the login for a person known to several tenants.
        logger.info("portal otp refused: %s maps to %d orgs", email, len(orgs))
        return accepted
    candidate = candidates[0]

    settings = get_settings()
    hour_ago = utcnow() - timedelta(hours=1)
    issued = (
        db.scalar(
            select(func.count())
            .select_from(PortalOtp)
            .where(PortalOtp.email == email, PortalOtp.created_at >= hour_ago)
        )
        or 0
    )
    if issued >= settings.portal_otp_hourly_cap:
        return accepted

    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(
        PortalOtp(
            org_id=candidate.org_id,
            candidate_id=candidate.id,
            email=email,
            code_hash=_hash_code(code),
            expires_at=utcnow() + timedelta(seconds=settings.portal_otp_ttl_seconds),
        )
    )
    db.flush()

    from_email, from_name = portal_sender()
    sender_for_org(db, candidate.org_id).send(
        OutboundEmail(
            to=email,
            subject=f"Your sign-in code: {code}",
            text_body=(
                f"Your sign-in code is {code}. It expires in "
                f"{settings.portal_otp_ttl_seconds // 60} minutes.\n\n"
                "If you didn't request this, you can ignore it."
            ),
            html_body="",
            from_email=from_email,
            from_name=from_name,
        )
    )
    return accepted


@router.post("/otp/verify")
def otp_verify(payload: OtpVerifyIn, db: Session = Depends(get_db)) -> dict:
    email = (payload.email or "").strip().lower()
    code = (payload.code or "").strip()
    if not email or not code:
        raise _UNAUTHORIZED

    now = utcnow()
    otp = db.scalar(
        select(PortalOtp)
        .where(PortalOtp.email == email, PortalOtp.consumed_at.is_(None))
        .order_by(PortalOtp.created_at.desc())
    )
    settings = get_settings()
    if otp is None or _aware(otp.expires_at) < now:
        raise _UNAUTHORIZED
    if otp.attempts >= settings.portal_otp_max_attempts:
        raise _UNAUTHORIZED

    # Committed BEFORE the comparison: the 401 a wrong guess raises rolls the
    # session back, and an attempt counter that rolls back with it would give
    # an attacker unlimited guesses. The spent attempt must survive the 401.
    otp.attempts += 1
    db.commit()
    if not hmac.compare_digest(otp.code_hash, _hash_code(code)):
        raise _UNAUTHORIZED

    otp.consumed_at = now
    candidate = db.get(Candidate, otp.candidate_id)
    if candidate is None:
        raise _UNAUTHORIZED
    # The audit row, same shape as a link grant: how this session was issued.
    db.add(
        CandidateAuthGrant(
            org_id=otp.org_id,
            candidate_id=otp.candidate_id,
            channel="otp",
            expires_at=now,
            consumed_at=now,
        )
    )
    db.flush()
    return {
        "access_token": mint_session_token(
            org_id=candidate.org_id, candidate_id=candidate.id
        ),
        "expires_in": settings.portal_session_ttl_seconds,
        "next": "/",
        "candidate": {"first_name": candidate.first_name or candidate.full_name or ""},
    }

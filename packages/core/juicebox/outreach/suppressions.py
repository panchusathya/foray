"""Internal do-not-contact list.

Outreach goes out as normal 1:1 email from the recruiter's own mailbox, so
there is no public unsubscribe flow. This list exists so a candidate who
bounced, or who replied asking to be left alone, is never contacted again —
sends to a suppressed address are refused before anything is rendered.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Suppression

SUPPRESSION_REASONS = frozenset({"unsubscribe", "bounce", "manual"})


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_suppressed(db: Session, *, org_id: str, email: str) -> bool:
    normalised = normalize_email(email)
    if not normalised:
        return False
    return (
        db.scalar(
            select(Suppression.id).where(
                Suppression.org_id == org_id, Suppression.email == normalised
            )
        )
        is not None
    )


def suppress(
    db: Session,
    *,
    org_id: str,
    email: str,
    reason: str = "unsubscribe",
    source: str = "manual",
    source_message_id: str = "",
) -> Suppression | None:
    """Idempotent. Empty addresses are ignored."""
    normalised = normalize_email(email)
    if not normalised:
        return None
    reason = reason if reason in SUPPRESSION_REASONS else "unsubscribe"
    existing = db.scalar(
        select(Suppression).where(Suppression.org_id == org_id, Suppression.email == normalised)
    )
    if existing is not None:
        return existing
    row = Suppression(
        org_id=org_id,
        email=normalised,
        reason=reason,
        source=source,
        source_message_id=source_message_id or "",
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        found = db.scalar(
            select(Suppression).where(
                Suppression.org_id == org_id, Suppression.email == normalised
            )
        )
        return found
    return row


def unsuppress(db: Session, *, org_id: str, email: str) -> bool:
    normalised = normalize_email(email)
    if not normalised:
        return False
    row = db.scalar(
        select(Suppression).where(Suppression.org_id == org_id, Suppression.email == normalised)
    )
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True

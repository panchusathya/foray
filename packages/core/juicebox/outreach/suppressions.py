"""Internal do-not-contact list.

Outreach goes out as normal 1:1 email from the recruiter's own mailbox, so
there is no public unsubscribe flow. This list exists so a candidate who
bounced, or who replied asking to be left alone, is never contacted again —
sends to a suppressed address are refused before anything is rendered.

Phone is the same list for voice and, later, SMS. A STOP from an unknown
number has no email; those rows are phone-only and ``email`` is null.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..intake.phones import lookup_key
from ..models import Suppression

SUPPRESSION_REASONS = frozenset({"unsubscribe", "bounce", "manual"})


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def is_suppressed(db: Session, *, org_id: str, email: str = "", phone: str = "") -> bool:
    normalised = normalize_email(email)
    if normalised:
        found = db.scalar(
            select(Suppression.id).where(
                Suppression.org_id == org_id, Suppression.email == normalised
            )
        )
        if found is not None:
            return True
    key = lookup_key(phone) if phone else ""
    if key:
        found = db.scalar(
            select(Suppression.id).where(Suppression.org_id == org_id, Suppression.phone == key)
        )
        if found is not None:
            return True
    return False


def _record_stronger_fact(
    row: Suppression, *, reason: str, source: str, source_message_id: str
) -> None:
    """Let a real opt-out overwrite a hand-set one on the same address.

    ``suppress`` is idempotent per address, so there is only ever one row, and
    without this a candidate who was already on the list as ``manual`` (a
    recruiter set do_not_contact) and then replied "unsubscribe" stayed
    recorded as manual -- which ``unsuppress`` is allowed to clear. Never the
    other way round: a status flip must not downgrade a bounce.
    """
    if reason == "manual" or row.reason != "manual":
        return
    row.reason = reason
    row.source = source
    if source_message_id:
        row.source_message_id = source_message_id


def suppress(
    db: Session,
    *,
    org_id: str,
    email: str = "",
    phone: str = "",
    reason: str = "unsubscribe",
    source: str = "manual",
    source_message_id: str = "",
) -> Suppression | None:
    """Idempotent. Empty addresses and unparseable phones are ignored."""
    normalised = normalize_email(email)
    key = lookup_key(phone) if phone else ""
    if not normalised and not key:
        return None
    reason = reason if reason in SUPPRESSION_REASONS else "unsubscribe"
    if normalised:
        existing = db.scalar(
            select(Suppression).where(
                Suppression.org_id == org_id, Suppression.email == normalised
            )
        )
        if existing is not None:
            if key and not existing.phone:
                existing.phone = key
            _record_stronger_fact(
                existing, reason=reason, source=source, source_message_id=source_message_id
            )
            return existing
    if key:
        existing = db.scalar(
            select(Suppression).where(Suppression.org_id == org_id, Suppression.phone == key)
        )
        if existing is not None:
            if normalised and not existing.email:
                existing.email = normalised
            _record_stronger_fact(
                existing, reason=reason, source=source, source_message_id=source_message_id
            )
            return existing
    row = Suppression(
        org_id=org_id,
        email=normalised or None,
        phone=key,
        reason=reason,
        source=source,
        source_message_id=source_message_id or "",
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        if normalised:
            return db.scalar(
                select(Suppression).where(
                    Suppression.org_id == org_id, Suppression.email == normalised
                )
            )
        return db.scalar(
            select(Suppression).where(Suppression.org_id == org_id, Suppression.phone == key)
        )
    return row


def unsuppress(
    db: Session,
    *,
    org_id: str,
    email: str = "",
    phone: str = "",
    only_reasons: Collection[str] | None = None,
) -> bool:
    """Take an address (or number) back off the list.

    ``only_reasons`` narrows what may be removed, and a caller undoing its own
    bookkeeping must pass it. ``suppress`` is idempotent per address, so an
    address has exactly one row and that row can perfectly well be somebody's
    bounce or their reply asking to be left alone: deleting whatever is there
    is how moving a contact off do_not_contact used to erase a genuine opt-out
    and make them mailable again. The operator-facing "remove from the list"
    action passes nothing, because that is a human deliberately overriding the
    fact.
    """
    row = None
    normalised = normalize_email(email)
    if normalised:
        row = db.scalar(
            select(Suppression).where(
                Suppression.org_id == org_id, Suppression.email == normalised
            )
        )
    if row is None:
        key = lookup_key(phone) if phone else ""
        if key:
            row = db.scalar(
                select(Suppression).where(Suppression.org_id == org_id, Suppression.phone == key)
            )
    if row is None:
        return False
    if only_reasons is not None and row.reason not in only_reasons:
        return False
    db.delete(row)
    db.flush()
    return True

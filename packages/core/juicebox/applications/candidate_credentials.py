"""Candidate-owned ATS credentials.

This is deliberately separate from ``applications.credentials``: that module
holds employer-issued board API keys.  These rows are candidate credentials and
may be decrypted only by a submitter immediately before it signs in.
"""

from __future__ import annotations

import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.crypto import decrypt_json, encrypt_json, needs_reencrypt
from ..models import AtsAccount, utcnow


def generated_password() -> str:
    """A board-compatible random password; it is never returned in an API."""
    return secrets.token_urlsafe(24) + "Aa1!"


def account_for(db: Session, *, candidate_id: str, vendor: str, tenant: str) -> AtsAccount | None:
    return db.scalar(
        select(AtsAccount).where(
            AtsAccount.candidate_id == candidate_id,
            AtsAccount.vendor == vendor,
            AtsAccount.tenant == tenant,
        )
    )


def read_password(db: Session, *, candidate_id: str, vendor: str, tenant: str) -> tuple[str, str]:
    """Return a usable credential or blanks, marking unreadable rows safely."""
    row = account_for(db, candidate_id=candidate_id, vendor=vendor, tenant=tenant)
    if row is None or not row.token_blob or row.revoked_at is not None:
        return "", ""
    # Legacy handovers intentionally discarded the final password.  They must
    # be re-entered through the step-up portal flow, never silently retried.
    if row.handed_over_at is not None or row.status in {"revoked", "needs_reentry"}:
        return "", ""
    try:
        payload = decrypt_json(row.token_blob)
    except ValueError:
        row.status = "needs_reentry"
        row.error = "credential can no longer be decrypted; ask the candidate to re-enter it"
        db.flush()
        return "", ""
    password = str(payload.get("password") or "")
    if not password:
        return "", ""
    if needs_reencrypt(row.token_blob):
        row.token_blob = encrypt_json({"password": password})
    row.last_used_at = utcnow()
    db.flush()
    return row.username or "", password


def save_password(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    vendor: str,
    tenant: str,
    username: str,
    password: str,
    origin: str,
    registered: bool = True,
) -> AtsAccount:
    """Create or replace one candidate credential after a vault step-up.

    ``registered`` says whether this login is known to exist on the employer.
    ``remember_account`` calls it True (an account we just made and verified);
    the vault prompt calls it False — the candidate picked a password we have
    not registered yet, so the account is ``pending_verification`` and
    ``verified_at`` stays null until a registration+verification actually
    lands. The submitter's register-vs-sign-in fork reads exactly that.
    """
    if not (vendor or "").strip() or not (tenant or "").strip() or not (username or "").strip():
        raise ValueError("vendor, tenant and username are required")
    if len(password or "") < 8:
        raise ValueError("password must be at least 8 characters")
    row = account_for(db, candidate_id=candidate_id, vendor=vendor, tenant=tenant)
    if row is None:
        row = AtsAccount(org_id=org_id, candidate_id=candidate_id, vendor=vendor, tenant=tenant)
        db.add(row)
    row.username = username.strip()[:255]
    row.token_blob = encrypt_json({"password": password})
    row.credential_origin = origin if origin in {"generated", "candidate_supplied"} else "candidate_supplied"
    row.status = "active" if registered else "pending_verification"
    row.error = ""
    row.handed_over_at = None
    row.revoked_at = None
    row.verified_at = utcnow() if registered else None
    db.flush()
    return row


def account_state(db: Session, *, candidate_id: str, vendor: str, tenant: str) -> tuple[str, bool]:
    """``(password, verified)`` for a stored login, or ``("", False)``.

    ``verified`` is whether the account is confirmed to exist on the employer
    (register with it if not, sign in if so). Shares ``read_password``'s
    unreadable/revoked handling so the two never disagree.
    """
    password = read_password(db, candidate_id=candidate_id, vendor=vendor, tenant=tenant)[1]
    if not password:
        return "", False
    row = account_for(db, candidate_id=candidate_id, vendor=vendor, tenant=tenant)
    return password, bool(row and row.verified_at is not None)


def revoke(db: Session, row: AtsAccount) -> None:
    """Destroy the secret while retaining an auditable, non-usable row."""
    row.token_blob = ""
    row.status = "revoked"
    row.error = "revoked by candidate"
    row.revoked_at = utcnow()
    db.flush()

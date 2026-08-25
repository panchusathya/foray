"""Employer board keys: store, read, and the rule about when they matter.

One function each way, so no adapter grows its own idea of where a key lives or
what to do without one. ``board_key`` returning empty is not an error -- it is
the ordinary state for the open market, and it is what routes a submission to
the hosted form instead of the API.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..integrations.crypto import decrypt_json, encrypt_json
from ..models import AtsBoardCredential, utcnow

logger = logging.getLogger(__name__)


def board_key(db: Session, *, org_id: str, vendor: str, tenant: str) -> str:
    """The employer's API key for this board, or empty if we hold none."""
    if not vendor or not tenant:
        return ""
    row = db.scalar(
        select(AtsBoardCredential).where(
            AtsBoardCredential.org_id == org_id,
            AtsBoardCredential.vendor == vendor,
            AtsBoardCredential.tenant == tenant,
            AtsBoardCredential.status == "active",
        )
    )
    if row is None:
        return ""
    try:
        return str(decrypt_json(row.token_blob).get("key") or "")
    except ValueError as exc:
        # A key encrypted under a rotated SECRET_KEY is unusable, and falling
        # back to the form is better than failing the submission outright.
        logger.warning("board credential %s is unreadable: %s", row.id, exc)
        return ""


def set_board_key(
    db: Session,
    *,
    org_id: str,
    vendor: str,
    tenant: str,
    key: str,
    label: str = "",
    created_by: str | None = None,
) -> AtsBoardCredential:
    """Store or replace one board key. Encrypted at rest, like every other."""
    row = db.scalar(
        select(AtsBoardCredential).where(
            AtsBoardCredential.org_id == org_id,
            AtsBoardCredential.vendor == vendor,
            AtsBoardCredential.tenant == tenant,
        )
    )
    if row is None:
        row = AtsBoardCredential(org_id=org_id, vendor=vendor, tenant=tenant)
        db.add(row)
    row.token_blob = encrypt_json({"key": key})
    row.label = label[:120]
    row.status = "active"
    row.created_by = created_by
    db.flush()
    logger.info("stored a %s board key for %s", vendor, tenant)
    return row


def mark_used(db: Session, *, org_id: str, vendor: str, tenant: str) -> None:
    row = db.scalar(
        select(AtsBoardCredential).where(
            AtsBoardCredential.org_id == org_id,
            AtsBoardCredential.vendor == vendor,
            AtsBoardCredential.tenant == tenant,
        )
    )
    if row is not None:
        row.last_used_at = utcnow()
        db.flush()

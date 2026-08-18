"""Outreach compliance: suppression list, unsubscribe links, CAN-SPAM footer.

This is the productised floor, not legal advice. Commercial email in the US
needs a physical postal address and a working opt-out; we refuse SMTP sends
that are missing the address, always attach an unsubscribe link, and honour
opt-outs on a per-org suppression list.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Org, Suppression

UNSUBSCRIBE_REASONS = frozenset({"unsubscribe", "bounce", "manual"})


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def postal_address_for(org: Org | None) -> str:
    if org is not None and (org.postal_address or "").strip():
        return org.postal_address.strip()
    return get_settings().outreach_postal_address.strip()


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
    reason = reason if reason in UNSUBSCRIBE_REASONS else "unsubscribe"
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


def unsubscribe_url(*, base_url: str, message_id: str, token: str) -> str:
    return f"{base_url.rstrip('/')}/t/u/{message_id}?k={token}"


def compliance_headers(unsub_url: str) -> dict[str, str]:
    return {
        "List-Unsubscribe": f"<{unsub_url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def append_footer(
    text_body: str,
    html_body: str,
    *,
    unsub_url: str,
    postal_address: str,
) -> tuple[str, str]:
    address = postal_address.strip()
    text_lines = [
        text_body.rstrip(),
        "",
        "---",
        "If this isn't relevant, unsubscribe here:",
        unsub_url,
    ]
    if address:
        text_lines.append(address)
    html_bits = [
        html_body,
        '<p style="color:#666;font-size:12px;line-height:1.4">',
        f'If this isn\'t relevant, <a href="{unsub_url}">unsubscribe</a>.',
    ]
    if address:
        html_bits.append(f"<br>{_escape(address)}")
    html_bits.append("</p>")
    return "\n".join(text_lines) + "\n", "".join(html_bits)


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

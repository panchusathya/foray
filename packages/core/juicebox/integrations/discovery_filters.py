"""Which Sent-Items recipients are worth staging as contacts.

Kept pure and separate because this is where the feature quietly fails. Filter
too little and the tray fills with vendors and no-reply robots until nobody
opens it; filter too much and a real person is dropped without trace. The
compromise is two tiers: hard rejects that are never people, and soft flags
that stay in the tray sorted last.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enrich.normalize import clean_emails
from ..models import MailboxConnection, User

# Never a person.
HARD_REJECT_PREFIXES = (
    "no-reply",
    "noreply",
    "no_reply",
    "do-not-reply",
    "donotreply",
    "mailer-daemon",
    "postmaster",
    "bounce",
    "bounces",
)

# Often a person, at a small company almost always one. Staged, flagged, and
# sorted last -- never silently dropped.
ROLE_ACCOUNT_PREFIXES = (
    "support",
    "info",
    "help",
    "hello",
    "contact",
    "admin",
    "billing",
    "invoices",
    "accounts",
    "security",
    "newsletter",
    "updates",
    "marketing",
    "careers",
    "jobs",
)

# Bulk senders that are infrastructure rather than correspondents.
AUTOMATED_DOMAIN_MARKERS = ("bounces.", ".sendgrid.net", ".mailgun.org", "sparkpostmail")

# Public mailbox hosts are never "our internal domain", even when the org's own
# mailbox is on one. A Gmail-hosted recruiter who did not have this list would
# filter out most of their own address book.
PUBLIC_EMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.co.uk",
        "live.com",
        "msn.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "mail.com",
        "zoho.com",
        "fastmail.com",
    }
)


def domain_of(address: str) -> str:
    _, _, domain = (address or "").partition("@")
    return domain.strip().lower()


def own_addresses(db: Session, *, org_id: str) -> set[str]:
    """Us: every connected mailbox and every user in the org.

    Both halves matter. Mailbox addresses catch mail between our own sending
    accounts; user emails catch a colleague who is on the To line but has no
    mailbox connected.
    """
    mailboxes = db.scalars(
        select(MailboxConnection.address).where(MailboxConnection.org_id == org_id)
    ).all()
    users = db.scalars(select(User.email).where(User.org_id == org_id)).all()
    return {(a or "").strip().lower() for a in [*mailboxes, *users] if a}


def internal_domains(db: Session, *, org_id: str, configured: list[str] | None = None) -> set[str]:
    """Corporate domains that count as "us", never public mailbox hosts."""
    domains = {domain_of(a) for a in own_addresses(db, org_id=org_id)}
    domains |= {(d or "").strip().lower() for d in (configured or [])}
    return {d for d in domains if d and d not in PUBLIC_EMAIL_DOMAINS}


def classify_recipient(
    address: str,
    *,
    own: set[str],
    internal: set[str],
    known_addresses: set[str] | None = None,
) -> str:
    """Empty string means keep. Anything else is the reason it was rejected.

    Ordered cheapest and most certain first.
    """
    email = (address or "").strip().lower()
    if not email or not clean_emails([email]):
        return "invalid"
    if email in own:
        return "self"

    local, _, domain = email.partition("@")
    if domain in internal:
        return "internal"
    if any(marker in domain for marker in AUTOMATED_DOMAIN_MARKERS):
        return "automated"

    # Strip the +tag before matching a prefix, or no-reply+123@ walks through.
    base = local.split("+", 1)[0]
    if base in HARD_REJECT_PREFIXES or base.startswith(("no-reply", "noreply")):
        return "automated"

    if known_addresses and email in known_addresses:
        # Not a rejection: staged as already_known so the tray can offer to
        # import history for people who are in Contacts but were never in a
        # project, a sequence, or any correspondence.
        return "known"
    return ""


def suggestion_for(*, local_part: str, to_count: int, is_suppressed: bool = False) -> str:
    """The soft flag shown next to a staged contact, from what is known."""
    if is_suppressed:
        return "suppressed"
    if to_count == 0:
        return "cc_only"
    if local_part.split("+", 1)[0] in ROLE_ACCOUNT_PREFIXES:
        return "role_account"
    return ""

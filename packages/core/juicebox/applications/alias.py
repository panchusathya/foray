"""The address an employer sees.

One mailbox we own, sub-addressed per application:
``anna+<token>@goforay.io``. The token is an HMAC of the application id
(``security.application_alias_token``), so an inbound message can be attributed
before any database lookup and a local part cannot be guessed into someone's
application inbox.

Older rows are scoped to the *candidate* instead, from before the alias was
minted per application. Employers are already writing to those, so they keep
resolving forever: ``resolve`` reads ``CandidateAlias.scope`` to decide which
HMAC has to agree.

**Why own the identity at all.** Every ATS that makes you register gates on a
click in an email, and a lot of them then send a one-time code. Reading the
candidate's own inbox to clear that means a Google restricted scope, a vendor's
verified client or our own CASA assessment, and an OAuth consent screen in the
middle of onboarding. Owning the endpoint removes all of it, and the employer's
replies -- which are what the tracker is made of -- arrive by construction
rather than by permission.

**What it costs, written down rather than discovered later.** We become the
addressee of record on every application. The employer can see the domain, so
we never claim otherwise. One shared mailbox is one sending reputation for
every candidate on it. And a candidate who hits Reply in their own client
answers from their own address, which the employer has no record of -- so the
relay pins ``Reply-To`` back to the alias and the portal is where a reply
belongs.

With ``APPLY_ALIAS_ADDRESS`` unset no alias is issued and the candidate's own
address goes on the application. That is honest and it is also the state in
which verification-gated portals stall.
"""

from __future__ import annotations

import logging
import re
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import Candidate, CandidateAlias, utcnow
from ..security import (
    alias_token,
    application_alias_token,
    verify_alias_token,
    verify_application_alias_token,
)

logger = logging.getLogger(__name__)

#: The separator every major provider treats as a sub-address.
SEPARATOR = "+"
_ADDRESS = re.compile(r"^(?P<local>[^@]+)@(?P<domain>[^@]+)$")


def mailbox() -> tuple[str, str]:
    """``(local_part, domain)`` of the mailbox that owns every alias."""
    configured = (get_settings().apply_alias_address or "").strip().lower()
    found = _ADDRESS.match(configured)
    if found is None:
        return "", ""
    return found.group("local"), found.group("domain")


def enabled() -> bool:
    return all(mailbox())


def address_for(candidate_id: str) -> str:
    """The alias for one candidate, or empty when aliasing is off."""
    local, domain = mailbox()
    if not local:
        return ""
    return f"{local}{SEPARATOR}{alias_token(candidate_id)}@{domain}"


def parse(address: str) -> str:
    """The token out of an alias address, or empty if it is not one of ours.

    Accepts a bare address or a full ``Name <addr>`` header value, because
    ``MailboxEvent.to_addresses`` carries whatever the provider gave us.
    """
    _, bare = parseaddr((address or "").strip())
    found = _ADDRESS.match((bare or address or "").strip().lower())
    if found is None:
        return ""
    local, domain = mailbox()
    if not local or found.group("domain") != domain:
        return ""
    head, separator, token = found.group("local").partition(SEPARATOR)
    if not separator or head != local:
        return ""
    return token


def resolve(db: Session, address: str) -> tuple[Candidate | None, str]:
    """``(candidate, application_id)`` for an alias address.

    The row is the discriminator, not the address. A candidate-scoped token and
    an application-scoped one are both eighteen alphanumeric characters in the
    same position and are deliberately indistinguishable by inspection, so the
    stored ``scope`` says which id the local part is an HMAC of and therefore
    which verifier has to agree.

    Verified rather than trusted either way: the address is attacker-supplied,
    and a row lookup on its own would make any stored alias addressable by
    anyone who learned it.
    """
    token = parse(address)
    if not token:
        return None, ""
    row = db.scalar(
        select(CandidateAlias).where(
            CandidateAlias.local_part == token, CandidateAlias.status == "active"
        )
    )
    if row is None:
        return None, ""

    if row.scope == "application":
        if not row.application_id or not verify_application_alias_token(
            row.application_id, token
        ):
            logger.warning("alias %s does not verify against application %s", address, row.application_id)
            return None, ""
        return db.get(Candidate, row.candidate_id), row.application_id or ""

    if not verify_alias_token(row.candidate_id, token):
        logger.warning("alias %s does not verify against candidate %s", address, row.candidate_id)
        return None, ""
    return db.get(Candidate, row.candidate_id), ""


def candidate_for(db: Session, address: str) -> Candidate | None:
    """Which candidate an alias address belongs to.

    The token is verified against the row's own candidate id rather than
    trusted: the address is attacker-supplied, and the row lookup alone would
    make any stored alias addressable by anyone who learned it.
    """
    token = parse(address)
    if not token:
        return None
    row = db.scalar(
        select(CandidateAlias).where(
            CandidateAlias.local_part == token, CandidateAlias.status == "active"
        )
    )
    if row is None:
        return None
    if not verify_alias_token(row.candidate_id, token):
        logger.warning("alias %s does not verify against candidate %s", address, row.candidate_id)
        return None
    return db.get(Candidate, row.candidate_id)


def ensure(db: Session, candidate: Candidate) -> CandidateAlias | None:
    """The candidate's alias row, issued on first use. ``None`` when off."""
    address = address_for(candidate.id)
    if not address:
        return None
    existing = db.scalar(select(CandidateAlias).where(CandidateAlias.candidate_id == candidate.id))
    if existing is not None:
        if existing.status != "active":
            existing.status = "active"
            existing.revoked_at = None
            db.flush()
        return existing
    row = CandidateAlias(
        org_id=candidate.org_id,
        candidate_id=candidate.id,
        address=address,
        local_part=alias_token(candidate.id),
    )
    db.add(row)
    db.flush()
    logger.info("issued application alias %s for candidate %s", address, candidate.id)
    return row


def revoke(db: Session, candidate_id: str) -> None:
    """Stop accepting mail for a candidate's alias.

    Does not delete it: mail addressed to a revoked alias should be identifiable
    as ours and dropped deliberately, not land in the unmatched bucket.
    """
    row = db.scalar(select(CandidateAlias).where(CandidateAlias.candidate_id == candidate_id))
    if row is None or row.status == "revoked":
        return
    row.status = "revoked"
    row.revoked_at = utcnow()
    db.flush()


def address_for_application(application_id: str) -> str:
    """The alias for one application, or empty when aliasing is off."""
    local, domain = mailbox()
    if not local:
        return ""
    return f"{local}{SEPARATOR}{application_alias_token(application_id)}@{domain}"


def ensure_for_application(db: Session, application) -> CandidateAlias | None:
    """The application's alias row, issued on first use. ``None`` when off.

    Reuses whatever this application already has, including a legacy
    candidate-scoped row from before aliases were minted per application --
    reissuing would change the address an employer is already writing to.
    """
    address = address_for_application(application.id)
    if not address:
        return None
    existing = db.scalar(
        select(CandidateAlias).where(CandidateAlias.application_id == application.id)
    )
    if existing is None:
        existing = db.scalar(
            select(CandidateAlias).where(
                CandidateAlias.candidate_id == application.candidate_id,
                CandidateAlias.scope == "candidate",
            )
        )
    if existing is not None:
        if existing.status != "active":
            existing.status = "active"
            existing.revoked_at = None
            db.flush()
        return existing

    row = CandidateAlias(
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        application_id=application.id,
        scope="application",
        address=address,
        local_part=application_alias_token(application.id),
    )
    db.add(row)
    db.flush()
    logger.info("issued application alias %s for application %s", address, application.id)
    return row


def sending_identity(db: Session, candidate: Candidate, application=None) -> tuple[str, str]:
    """``(from_address, reply_to_address)`` for one application.

    With aliasing on, the employer sees the alias and the candidate's real
    address is only ever a relay destination. With it off, the candidate's own
    address is both, and no verification loop can be cleared for them.

    ``application`` is optional only so a caller without one still gets the
    candidate-scoped behaviour; every real submission has one, and passing it
    is what makes an employer's reply identify the pursuit exactly.
    """
    real = primary_email(candidate)
    row = ensure_for_application(db, application) if application is not None else ensure(db, candidate)
    if row is None:
        return real, real
    return row.address, real


def primary_email(candidate: Candidate) -> str:
    """The candidate's own address, whatever shape the emails list is in."""
    for entry in candidate.emails or []:
        if isinstance(entry, str) and "@" in entry:
            return entry.strip()
        if isinstance(entry, dict):
            value = str(entry.get("email") or entry.get("address") or "").strip()
            if "@" in value:
                return value
    return ""

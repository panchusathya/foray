"""Warm apply-browser profiles: load, filter, persist.

The driver stays database-free (the adapter rule, one layer down), so this
module is the seam: the submission service loads a ``ProfileState`` here,
passes it through ``SubmissionContext``, the adapter hands it to
``driver.session(profile=...)``, and the service persists what the run saved.

**The origin filter is the control, not the encryption.** A persisted
``storage_state`` holding a live session is a credential, and anything the
browsing agent can reach is reachable by an instruction injected into a page
it loads. So a run never receives the whole jar: cookies and origins are cut
down to the hosts this run actually visits, and a profile holding a session
for host A releases nothing to a run against host B. Encryption at rest
(``integrations/crypto.py``, the OAuth-token treatment) is the floor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from ....integrations.crypto import decrypt_json, encrypt_json
from ....models import BrowserProfile, utcnow

logger = logging.getLogger(__name__)


@dataclass
class ProfileState:
    """What one run may know about the candidate's browser identity.

    A plain value object on purpose -- it crosses the adapter boundary inside
    ``SubmissionContext``, and an adapter must not reach the database.
    ``saved_state`` is the return channel: the driver writes the context's
    closing ``storage_state`` here, and the service persists it.
    """

    profile_id: str = ""
    #: Origin-filtered Playwright storage_state for THIS run's hosts, or None.
    storage_state: dict | None = None
    #: Sticky proxy session token -- one candidate, one egress IP.
    egress_session: str = ""
    user_agent: str = ""
    #: Written by the driver on a clean close; never read by it.
    saved_state: dict | None = field(default=None, repr=False)


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").split(":")[0].lower()
    except ValueError:
        return ""


def _domain_allows(cookie_domain: str, hosts: set[str]) -> bool:
    """Cookie-domain matching: ``.example.com`` covers ``jobs.example.com``."""
    bare = cookie_domain.lstrip(".").lower()
    if not bare:
        return False
    for host in hosts:
        if host == bare or host.endswith("." + bare) or bare.endswith("." + host):
            return True
    return False


def filtered_state(state: dict | None, *, hosts: set[str]) -> dict | None:
    """The storage_state cut down to what one run's hosts may see."""
    if not isinstance(state, dict):
        return None
    hosts = {h for h in hosts if h}
    if not hosts:
        return None
    cookies = [
        c
        for c in state.get("cookies") or []
        if isinstance(c, dict) and _domain_allows(str(c.get("domain") or ""), hosts)
    ]
    origins = [
        o
        for o in state.get("origins") or []
        if isinstance(o, dict) and _domain_allows(_host(str(o.get("origin") or "")), hosts)
    ]
    if not cookies and not origins:
        return None
    return {"cookies": cookies, "origins": origins}


def profile_for(
    db: Session, *, org_id: str, candidate_id: str, run_hosts: set[str]
) -> ProfileState | None:
    """This candidate's profile, filtered for one run. Creates the row.

    Returns ``None`` when there is nobody to key a profile on -- the caller
    falls back to today's cold context, which is always a safe answer.
    """
    if not candidate_id:
        return None
    row = db.scalar(
        select(BrowserProfile).where(
            BrowserProfile.org_id == org_id,
            BrowserProfile.candidate_id == candidate_id,
        )
    )
    if row is None:
        import secrets

        row = BrowserProfile(
            org_id=org_id,
            candidate_id=candidate_id,
            egress_session=secrets.token_hex(6),
        )
        db.add(row)
        db.flush()
    stored: dict | None = None
    if row.storage_state:
        try:
            stored = decrypt_json(row.storage_state)
        except Exception:  # noqa: BLE001 - an unreadable blob is an empty profile
            # Same contract as submission._unseal: registering a fresh state is
            # recoverable, running with one we cannot read is not.
            logger.warning("browser profile %s no longer decrypts; starting fresh", row.id)
            stored = None
    return ProfileState(
        profile_id=row.id,
        storage_state=filtered_state(stored, hosts=run_hosts),
        egress_session=row.egress_session,
        user_agent=row.user_agent,
    )


def persist(db: Session, profile: ProfileState, *, user_agent: str = "") -> None:
    """Keep what the run saved, merged over what the row already holds.

    The run only ever saw a filtered slice, so its closing state is merged
    onto the stored jar rather than replacing it: host B's session must not
    vanish because a run against host A saved last. Merge key is
    (name, domain, path) for cookies and origin for origins -- the run's
    version wins where both hold one.
    """
    if not profile.profile_id or profile.saved_state is None:
        return
    row = db.get(BrowserProfile, profile.profile_id)
    if row is None:
        return
    stored: dict = {"cookies": [], "origins": []}
    if row.storage_state:
        try:
            stored = decrypt_json(row.storage_state)
        except Exception:  # noqa: BLE001
            stored = {"cookies": [], "origins": []}

    def _cookie_key(c: dict) -> tuple:
        return (str(c.get("name")), str(c.get("domain")), str(c.get("path")))

    cookies = {_cookie_key(c): c for c in stored.get("cookies") or [] if isinstance(c, dict)}
    for c in profile.saved_state.get("cookies") or []:
        if isinstance(c, dict):
            cookies[_cookie_key(c)] = c
    origins = {
        str(o.get("origin")): o for o in stored.get("origins") or [] if isinstance(o, dict)
    }
    for o in profile.saved_state.get("origins") or []:
        if isinstance(o, dict):
            origins[str(o.get("origin"))] = o

    row.storage_state = encrypt_json(
        {"cookies": list(cookies.values()), "origins": list(origins.values())}
    )
    if user_agent and not row.user_agent:
        row.user_agent = user_agent[:400]
    row.last_used_at = utcnow()
    db.flush()

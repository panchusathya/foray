"""Find people we have emailed who are not in Contacts yet.

The live sync baselines at connect time and reads no history; the existing
importer only looks up addresses we already know. Neither can discover anyone,
so years of correspondence sit in the mailbox invisible to the CRM.

This walks Sent Items backwards over a bounded window and stages the external
recipients. **Sent only** -- twelve months of Inbox is typically 10-50x the
volume and is mostly newsletters and inbound recruiters, and "everyone we have
reached out to" is a Sent premise anyway.

**Metadata only.** Bodies and subjects are never requested from the provider
(see ``SweepRecord``), so an unpromoted stranger exists here as an address, a
name, counts and dates. Their actual mail is fetched only after a human
promotes them, at which point they are a candidate like any other.

Progress and the page cursor live in ``connection.discovery_state``. The cursor
must never be written to the live delta columns: doing so makes the ongoing
sync permanently re-read or permanently skip mail.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Candidate,
    CandidateEmail,
    DiscoveredContact,
    MailboxConnection,
    Suppression,
    utcnow,
)
from .discovery_filters import (
    classify_recipient,
    domain_of,
    internal_domains,
    own_addresses,
    suggestion_for,
)
from .providers import SweepRecord, get_provider

logger = logging.getLogger(__name__)

# A message with more external recipients than this is a blast, not a
# relationship. Rejects the *message*, not the addresses on it.
MAX_RECIPIENTS = 10

# How long since the last exchange before a relationship reads as revivable.
STALE_DAYS = 90


def window_start(now: datetime | None = None) -> datetime:
    now = now or utcnow()
    return now - timedelta(days=get_settings().mailbox_backfill_days)


def _suppressed(db: Session, *, org_id: str) -> set[str]:
    return set(
        db.scalars(select(Suppression.email).where(Suppression.org_id == org_id)).all()
    )


def display_name_from(email: str) -> str:
    """Best effort from the local part: "ada.lovelace" -> "Ada Lovelace"."""
    local = (email or "").partition("@")[0]
    parts = [p for p in local.replace("_", ".").replace("-", ".").split(".") if p]
    if not parts or any(any(ch.isdigit() for ch in p) for p in parts):
        return ""
    return " ".join(p.capitalize() for p in parts)


def build_context(db: Session, *, org_id: str, now: datetime) -> dict:
    """Everything the per-message loop needs, resolved once per job.

    Hoisted deliberately: these are whole-table reads, and doing them per page
    (let alone per message) would make the sweep quadratic.
    """
    known_rows = db.execute(
        select(CandidateEmail.email, CandidateEmail.candidate_id).where(
            CandidateEmail.org_id == org_id
        )
    ).all()
    return {
        "own": own_addresses(db, org_id=org_id),
        "internal": internal_domains(db, org_id=org_id),
        "known": {email: candidate_id for email, candidate_id in known_rows},
        "suppressed": _suppressed(db, org_id=org_id),
        "threads": {},
        "stale_before": now - timedelta(days=STALE_DAYS),
    }


def _observations(record: SweepRecord, context: dict) -> list[tuple[str, bool]]:
    """(address, was_on_the_to_line) for the recipients worth staging."""
    to = {a.strip().lower() for a in record.to_addresses if a}
    cc = {a.strip().lower() for a in record.cc_addresses if a}
    everyone = to | cc

    external = [
        a
        for a in everyone
        if classify_recipient(a, own=context["own"], internal=context["internal"])
        not in ("self", "internal")
    ]
    if len(external) > MAX_RECIPIENTS:
        # A 40-person Cc is an announcement. Drop the message rather than the
        # people: they may still be reachable through a one-to-one thread.
        return []

    out: list[tuple[str, bool]] = []
    for address in sorted(everyone):
        reason = classify_recipient(
            address,
            own=context["own"],
            internal=context["internal"],
            known_addresses=set(context["known"]),
        )
        # "known" is staged on purpose -- see stage_record.
        if reason and reason != "known":
            continue
        out.append((address, address in to))
    return out


def stage_record(
    db: Session,
    *,
    connection: MailboxConnection,
    record: SweepRecord,
    context: dict,
) -> int:
    """Fold one sent message into the staging table. Returns rows touched."""
    if record.is_draft or record.sent_at is None:
        return 0

    touched = 0
    for address, on_to_line in _observations(record, context):
        row = db.scalar(
            select(DiscoveredContact).where(
                DiscoveredContact.org_id == connection.org_id,
                DiscoveredContact.email == address,
            )
        )
        if row is None:
            owner = context["known"].get(address)
            row = DiscoveredContact(
                org_id=connection.org_id,
                email=address,
                domain=domain_of(address),
                display_name=display_name_from(address),
                # Already in Contacts but never in a project, sequence or
                # thread, so their mail has never been ingested. Kept so the
                # tray can offer to import it.
                status="already_known" if owner else "new",
                candidate_id=owner,
            )
            db.add(row)
            db.flush()
        elif row.status in ("promoted", "dismissed"):
            # A human decided about this person already. Re-running a sweep
            # must not resurrect a vendor somebody dismissed.
            continue

        row.outbound_count += 1
        if on_to_line:
            row.to_count += 1
        else:
            row.cc_count += 1

        seen = set(row.mailbox_ids or [])
        if connection.id not in seen:
            row.mailbox_ids = sorted([*seen, connection.id])

        when = record.sent_at
        if row.first_seen_at is None or when < row.first_seen_at:
            row.first_seen_at = when
        if row.last_seen_at is None or when > row.last_seen_at:
            row.last_seen_at = when

        if record.thread_id:
            threads = context["threads"].setdefault(address, set())
            if record.thread_id not in threads:
                threads.add(record.thread_id)
                row.thread_count = len(threads)

        flag = suggestion_for(
            local_part=address.partition("@")[0],
            to_count=row.to_count,
            is_suppressed=address in context["suppressed"],
        )
        if not flag and row.last_seen_at and row.last_seen_at < context["stale_before"]:
            flag = "revive"
        row.suggested_status = flag
        touched += 1
    return touched


def sweep_page(
    db: Session,
    connection: MailboxConnection,
    *,
    context: dict,
    since: datetime,
    cursor: str = "",
    provider=None,
) -> dict:
    """One provider page. Returns counters plus the next cursor ('' = done)."""
    provider = provider or get_provider(connection.provider)
    page = provider.iter_sent_history(db, connection, since=since, cursor=cursor)

    found = 0
    for record in page.records:
        found += stage_record(db, connection=connection, record=record, context=context)

    return {
        "scanned": len(page.records),
        "found": found,
        "next_cursor": page.next_cursor if page.has_more else "",
    }


def reset_for_rerun(db: Session, *, org_id: str) -> int:
    """Zero the counters on rows nobody has decided about yet.

    Counters accumulate, so a second sweep over the same window would double
    them. Rows already promoted or dismissed are left entirely alone.
    """
    rows = db.scalars(
        select(DiscoveredContact).where(
            DiscoveredContact.org_id == org_id,
            DiscoveredContact.status.in_(("new", "already_known")),
        )
    ).all()
    for row in rows:
        row.outbound_count = 0
        row.to_count = 0
        row.cc_count = 0
        row.thread_count = 0
        row.mailbox_ids = []
        row.first_seen_at = None
        row.last_seen_at = None
    db.flush()
    return len(rows)


# --------------------------------------------------------------------------
# sweep progress, kept on the connection
# --------------------------------------------------------------------------


def start_state(*, since: datetime, now: datetime) -> dict:
    return {
        "status": "running",
        "scanned": 0,
        "found": 0,
        "cursor": "",
        "since": since.isoformat(),
        "started_at": now.isoformat(),
        "finished_at": None,
        "error": "",
    }


def advance_state(connection: MailboxConnection, result: dict) -> None:
    """Fold a page's counters in, reassigning the JSON dict.

    Reassigned rather than edited in place: SQLAlchemy does not notice an
    in-place mutation of a JSON column, and a silently unsaved cursor means the
    sweep re-reads the same page forever.
    """
    state = dict(connection.discovery_state or {})
    state["scanned"] = int(state.get("scanned") or 0) + result["scanned"]
    state["found"] = int(state.get("found") or 0) + result["found"]
    state["cursor"] = result["next_cursor"]
    if not result["next_cursor"]:
        state["status"] = "done"
        state["finished_at"] = utcnow().isoformat()
    connection.discovery_state = state


def fail_state(connection: MailboxConnection, error: str) -> None:
    state = dict(connection.discovery_state or {})
    state["status"] = "failed"
    state["error"] = error[:500]
    connection.discovery_state = state


# --------------------------------------------------------------------------
# review decisions
# --------------------------------------------------------------------------


def promote(
    db: Session,
    *,
    row: DiscoveredContact,
    org_id: str,
    crm_status: str = "",
    now: datetime | None = None,
) -> tuple[str | None, str]:
    """Turn a staged contact into a real one. Returns (candidate_id, error).

    Accepts ``already_known`` as well as ``new``. Staging keeps people who are
    already in Contacts precisely so the tray can offer to import their
    history; if promoting them were a no-op, that offer would be a lie and
    ``crm_tracked`` would never be set, so the import would find nothing.
    """
    from ..crm import track
    from ..repository import owner_of_email, upsert_candidates

    now = now or utcnow()
    if row.status not in ("new", "already_known"):
        return None, f"already {row.status}"

    candidate_id = row.candidate_id or owner_of_email(db, org_id=org_id, email=row.email)
    if candidate_id is None:
        name = row.display_name or display_name_from(row.email) or row.email
        result = upsert_candidates(
            db,
            org_id=org_id,
            payloads=[
                {
                    "source": "mailbox",
                    "source_id": row.email,
                    "full_name": name,
                    "emails": [row.email],
                    "current_company_domain": row.domain,
                    "raw": {"discovered_from": "mailbox_sweep"},
                }
            ],
        )
        if not result.ids:
            # normalize_candidate raises on an unusable name and
            # upsert_candidates swallows it, so a bulk promote would drop rows
            # in silence. Report it instead of returning a success.
            return None, "could not be created (unusable name)"
        candidate_id = result.ids[0]

    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        return None, "candidate no longer exists"
    # This is what makes the follow-up import find anything: a promoted person
    # has no project, sequence or message, so without the flag the privacy
    # guard would keep discarding their mail.
    track(candidate)
    if crm_status:
        candidate.crm_status = crm_status

    row.status = "promoted"
    row.candidate_id = candidate_id
    row.promoted_at = now
    db.flush()
    return candidate_id, ""


def dismiss(
    db: Session, *, row: DiscoveredContact, reason: str = "", now: datetime | None = None
) -> None:
    row.status = "dismissed"
    row.dismissed_reason = (reason or "")[:60]
    row.dismissed_at = now or utcnow()
    db.flush()

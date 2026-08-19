"""Bounded historical import: past Outlook threads for known candidates.

The delta cursor deliberately baselines at connect time and reads no history,
so old correspondence needs its own pass. This one is per-candidate, not
per-mailbox: it iterates the same in-scope address index the live sync uses
(inheriting its privacy guard exactly) and asks Graph for messages exchanged
with each address, windowed to MAILBOX_BACKFILL_DAYS client-side because
``$search`` forbids ``$filter``.

Runs as a self-re-enqueueing worker job in batches, so one 429 storm or a
worker restart resumes instead of starting over. Every message goes through
the same idempotent ingest as the live sync — re-running is free.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MailboxConnection, utcnow
from .matching import build_address_index, record_synced_message, resolve_candidate
from .microsoft import GraphClient, MicrosoftError
from .sync import _already_recorded, _from_address, _recipient_addresses

logger = logging.getLogger(__name__)

BATCH_ADDRESSES = 25


def _within_window(payload: dict, *, since: datetime) -> bool:
    stamp = payload.get("sentDateTime") or payload.get("receivedDateTime") or ""
    if not stamp:
        return False
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    return when >= since


def backfill_mailbox_history(
    db: Session,
    connection: MailboxConnection,
    *,
    offset: int = 0,
    client: GraphClient | None = None,
    now: datetime | None = None,
) -> dict:
    """One batch of addresses. Returns counters plus next_offset (None = done)."""
    now = now or utcnow()
    if not connection.thread_sync_enabled:
        return {"ingested": 0, "skipped": 0, "next_offset": None, "reason": "disabled"}

    client = client or GraphClient(db, connection)
    since = now - timedelta(days=get_settings().mailbox_backfill_days)
    index = build_address_index(db, org_id=connection.org_id)
    addresses = sorted(index)[offset : offset + BATCH_ADDRESSES]
    if not addresses:
        return {"ingested": 0, "skipped": 0, "next_offset": None}

    ingested = 0
    skipped = 0
    for address in addresses:
        try:
            found = client.search_participant(address)
        except MicrosoftError as exc:
            # One bad address must not sink the batch; the job retries later.
            logger.warning("backfill search failed for %s: %s", address, exc)
            continue
        for payload in found:
            if payload.get("isDraft") or not _within_window(payload, since=since):
                skipped += 1
                continue
            if _already_recorded(
                db, org_id=connection.org_id, graph_message_id=payload.get("id", "")
            ):
                skipped += 1
                continue
            sender = _from_address(payload)
            outbound = sender == connection.address.lower()
            counterparty = (
                resolve_candidate(index, _recipient_addresses(payload))
                if outbound
                else resolve_candidate(index, [sender])
            )
            if counterparty is None:
                skipped += 1
                continue
            record_synced_message(
                db,
                org_id=connection.org_id,
                candidate_id=counterparty,
                mailbox_id=connection.id,
                direction="outbound" if outbound else "inbound",
                from_address=sender or connection.address,
                to_address=address if outbound else connection.address,
                payload=payload,
                now=now,
            )
            ingested += 1

    next_offset = offset + BATCH_ADDRESSES
    return {
        "ingested": ingested,
        "skipped": skipped,
        "next_offset": next_offset if next_offset < len(index) else None,
    }

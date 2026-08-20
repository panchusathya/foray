"""Bounded historical import: past threads for known candidates.

The live sync deliberately baselines at connect time and reads no history, so
old correspondence needs its own pass. This one is per-candidate, not
per-mailbox: it iterates the same in-scope address index the live sync uses
(inheriting its privacy guard exactly) and asks the provider for messages
exchanged with each address, windowed to MAILBOX_BACKFILL_DAYS on our side
because neither provider's search reliably filters by date.

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
from .providers import ProviderError, get_provider
from .sync import _already_recorded

logger = logging.getLogger(__name__)

BATCH_ADDRESSES = 25


def backfill_mailbox_history(
    db: Session,
    connection: MailboxConnection,
    *,
    offset: int = 0,
    addresses: list[str] | None = None,
    provider=None,
    now: datetime | None = None,
) -> dict:
    """One batch of addresses. Returns counters plus next_offset (None = done).

    ``addresses`` narrows the pass to exactly those people and skips the index
    walk. Promotion uses it: re-running the offset walk after promoting two
    contacts would ``$search`` every address in the org, one HTTP call each,
    to import two inboxes' worth of history. When it is None the original
    whole-index behaviour is unchanged.
    """
    now = now or utcnow()
    if not connection.thread_sync_enabled:
        return {"ingested": 0, "skipped": 0, "next_offset": None, "reason": "disabled"}

    _explicit = addresses is not None
    provider = provider or get_provider(connection.provider)
    since = now - timedelta(days=get_settings().mailbox_backfill_days)
    index = build_address_index(db, org_id=connection.org_id)
    if addresses is not None:
        # Still filtered through the index: an address the guard does not
        # admit has no business being ingested just because a caller asked.
        targets = [a for a in (a.strip().lower() for a in addresses) if a in index]
    else:
        targets = sorted(index)[offset : offset + BATCH_ADDRESSES]
    addresses = targets
    if not addresses:
        return {"ingested": 0, "skipped": 0, "next_offset": None}

    mailbox_address = connection.address.lower()
    ingested = 0
    skipped = 0
    for address in addresses:
        try:
            found = provider.search_participant(db, connection, address)
        except ProviderError as exc:
            # One bad address must not sink the batch; the job retries later.
            logger.warning("backfill search failed for %s: %s", address, exc)
            continue
        for event in found:
            when = event.occurred_at
            if event.is_draft or event.removed or when is None or when < since:
                skipped += 1
                continue
            if _already_recorded(
                db, org_id=connection.org_id, native_id=event.native_id
            ):
                skipped += 1
                continue
            outbound = event.from_address == mailbox_address
            counterparty = resolve_candidate(
                index,
                list(event.to_addresses) if outbound else [event.from_address],
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
                from_address=event.from_address or connection.address,
                to_address=address if outbound else connection.address,
                event=event,
                now=now,
                source=connection.provider,
            )
            ingested += 1

    if targets is not None and offset == 0 and _explicit:
        # An explicit address list is one pass, not a walk.
        return {"ingested": ingested, "skipped": skipped, "next_offset": None}
    next_offset = offset + BATCH_ADDRESSES
    return {
        "ingested": ingested,
        "skipped": skipped,
        "next_offset": next_offset if next_offset < len(index) else None,
    }

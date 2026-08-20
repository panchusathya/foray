"""Which of the org's mailboxes sends the next email.

One mailbox is one sending reputation, so raising outreach volume means
spreading it, not pushing harder on a single address. Every eligible mailbox
carries its own daily cap; this module picks the least-utilised one that still
has room and reports honestly when none does.

Two deliberate choices:

* The window is a rolling 24 hours, not a calendar day. A calendar day makes
  the whole pool refill at midnight and sends a burst that looks exactly like
  what spam filters are built to catch. It also needs a timezone per mailbox,
  which nobody would ever set correctly. (The org-wide cap in sequencer.py is
  still calendar-day; it is a spend guard, not a deliverability one.)
* A cap of zero or less makes a mailbox ineligible rather than unlimited.
  Someone typing 0 means "stop sending from this one".

Running out of room is not an error. The caller defers the job and tries again
later -- see ``process_enrollment``, which returns "deferred" so the queue
requeues the same permanent idempotency key rather than failing the step.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import MailboxConnection, Message

WINDOW_HOURS = 24


def _can_send_via(provider_name: str) -> bool:
    """Whether this deployment can actually send through a provider.

    Stored tokens are useless without client credentials -- the refresh call
    needs them -- so an unconfigured provider's mailboxes must not enter the
    rotation. The test seam counts as configured: injecting a sender is exactly
    a claim that sending works.
    """
    from ..integrations.providers import ProviderError, get_provider
    from .sender import has_sender_factory

    if has_sender_factory(provider_name):
        return True
    try:
        return get_provider(provider_name).is_configured()
    except ProviderError:
        return False


def eligible_mailboxes(db: Session, *, org_id: str) -> list[MailboxConnection]:
    """Mailboxes that could send, ignoring how much they have sent already.

    Ordered by id so the pick is deterministic when utilisation ties, which is
    what makes the rotation testable.
    """
    rows = db.scalars(
        select(MailboxConnection)
        .where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.status == "active",
            MailboxConnection.send_enabled.is_(True),
            MailboxConnection.daily_send_cap > 0,
        )
        .order_by(MailboxConnection.id)
    ).all()
    return [row for row in rows if _can_send_via(row.provider)]


def sent_in_window(db: Session, *, mailbox_id: str, now: datetime) -> int:
    """Sends actually accepted by the provider in the last WINDOW_HOURS.

    Counts status "sent" only: a queued row has not consumed reputation and a
    failed one never left, so neither should hold a slot.
    """
    if not mailbox_id:
        return 0
    since = now - timedelta(hours=WINDOW_HOURS)
    return int(
        db.scalar(
            select(func.count(Message.id)).where(
                Message.mailbox_id == mailbox_id,
                Message.direction == "outbound",
                Message.status == "sent",
                Message.sent_at >= since,
            )
        )
        or 0
    )


def remaining_capacity(db: Session, *, connection: MailboxConnection, now: datetime) -> int:
    return max(0, connection.daily_send_cap - sent_in_window(db, mailbox_id=connection.id, now=now))


def is_over_cap(db: Session, *, connection: MailboxConnection, now: datetime) -> bool:
    return remaining_capacity(db, connection=connection, now=now) <= 0


def pool_capacity(db: Session, *, org_id: str) -> int:
    """The org's combined daily headroom, used to lift the org-wide cap.

    Without this, five mailboxes capped at 40 each sit under a 200-send org cap
    and the pool silently defers everything the moment it is fully used --
    rotation that unlocks nothing.
    """
    return sum(row.daily_send_cap for row in eligible_mailboxes(db, org_id=org_id))


def pick_mailbox(
    db: Session, *, org_id: str, now: datetime
) -> MailboxConnection | None:
    """The least-utilised mailbox with room left, or None when all are full.

    None does not distinguish "no mailboxes" from "every mailbox is capped";
    callers that need to tell those apart ask ``eligible_mailboxes`` first, and
    they mean different things: the first falls through to the globally
    configured provider, the second defers.
    """
    best: MailboxConnection | None = None
    best_load = 1.0
    for connection in eligible_mailboxes(db, org_id=org_id):
        used = sent_in_window(db, mailbox_id=connection.id, now=now)
        if used >= connection.daily_send_cap:
            continue
        load = used / connection.daily_send_cap
        if best is None or load < best_load:
            best, best_load = connection, load
    return best

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

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import MailboxConnection, Message

WINDOW_HOURS = 24


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def gap_seconds(db: Session, org_id: str | None) -> tuple[int, int]:
    """(min, max) seconds between two automated emails on one domain.

    The org's own setting wins (a fixed number, or off); otherwise the
    deployment default. A deployment default of 0 switches spacing off
    everywhere, which is what the test suite runs with.
    """
    from ..models import Org

    settings = get_settings()
    low = max(settings.outreach_send_spacing_seconds, 0)
    high = max(settings.outreach_send_spacing_max_seconds, low)
    if high == 0:
        return 0, 0
    org = db.get(Org, org_id) if org_id else None
    if org is not None and getattr(org, "send_gap_enabled", True) is False:
        return 0, 0
    own = getattr(org, "send_gap_seconds", None) if org is not None else None
    if own is not None:
        own = max(int(own), 0)
        return own, own
    return low, high


def send_gap(*parts: object, low: int, high: int) -> timedelta:
    """The gap in front of one particular send: somewhere in [low, high].

    A hash rather than ``random`` so the worker and the Outbox preview agree.
    """
    if high <= low:
        return timedelta(seconds=max(low, 0))
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return timedelta(seconds=low + int(digest[:8], 16) % (high - low + 1))


def domain_of(address: str | None) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def domain_mailboxes(db: Session, org_id: str, domain: str) -> list[MailboxConnection]:
    rows = db.scalars(select(MailboxConnection).where(MailboxConnection.org_id == org_id)).all()
    return [row for row in rows if domain_of(row.address) == domain]


def domain_last_sent_at(
    db: Session, org_id: str, domain: str, now: datetime | None = None
) -> datetime | None:
    """The newest automated send from any mailbox on this domain.

    With ``now``, a stamp later than now is ignored. Every mailbox on the
    domain waits on this one clock, so a single message filed with a future
    timestamp (a provider reporting local time as UTC, say) would hold all of
    them until that moment -- which is how one manual reply stopped twenty
    mailboxes for six hours. Something that has not happened yet cannot have
    started a gap.
    """
    boxes = domain_mailboxes(db, org_id, domain)
    if not boxes:
        return None
    stamps = [_aware(b.last_sent_at) for b in boxes]
    query = select(func.max(Message.sent_at)).where(
        Message.mailbox_id.in_([b.id for b in boxes]),
        Message.direction == "outbound",
        Message.status == "sent",
    )
    if now is not None:
        query = query.where(Message.sent_at <= now)
    stamps.append(_aware(db.scalar(query)))
    return max((v for v in stamps if v is not None and (now is None or v <= now)), default=None)


def mailbox_gap_wait(
    db: Session, connection: MailboxConnection, now: datetime
) -> datetime | None:
    """When this mailbox may send again, if that is later than now.

    The clock is the sending DOMAIN's, shared by every mailbox on it: mail
    providers rate a domain, not a mailbox, and twenty mailboxes each sending
    once a minute read to them as one domain sending twenty a minute.
    """
    low, high = gap_seconds(db, connection.org_id)
    if high <= 0:
        return None
    domain = domain_of(connection.address)
    last = domain_last_sent_at(db, connection.org_id, domain, now)
    if last is None:
        return None
    opens = last + send_gap(domain, int(last.timestamp()), low=low, high=high)
    return opens if opens > now else None


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


def active_mailboxes(
    db: Session, *, org_id: str, only: list[str] | None = None
) -> list[MailboxConnection]:
    """Every live connection, regardless of whether it currently has room.

    ``only`` narrows the pool to a sequence's chosen mailboxes. None (and an
    empty list, which is how "all of them" is stored) means no restriction.
    Ids that no longer exist are ignored rather than raising: a mailbox can be
    disconnected long after a sequence named it, and that should stop it being
    picked, not break every send on the sequence.

    The distinction from ``eligible_mailboxes`` decides what happens when
    nothing can send right now. No live connection at all means this org never
    connected a mailbox (or revoked the last one), so sends fall through to the
    globally configured provider exactly as they did before the pool existed. A
    live connection that is merely switched off or capped out is a pause, and
    falling back there would send from the wrong identity -- so that defers
    instead.
    """
    query = (
        select(MailboxConnection)
        .where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.status == "active",
        )
        .order_by(MailboxConnection.id)
    )
    if only:
        query = query.where(MailboxConnection.id.in_(list(only)))
    return list(db.scalars(query).all())


def eligible_mailboxes(
    db: Session, *, org_id: str, only: list[str] | None = None
) -> list[MailboxConnection]:
    """Mailboxes that could send, ignoring how much they have sent already.

    Ordered by id so the pick is deterministic when utilisation ties, which is
    what makes the rotation testable.
    """
    return [
        row
        for row in active_mailboxes(db, org_id=org_id, only=only)
        if row.send_enabled and row.daily_send_cap > 0 and _can_send_via(row.provider)
    ]


def _seed_sends_since(db: Session, mailbox_id: str, since: datetime) -> list[datetime]:
    """Seed tests this mailbox sent since ``since`` (deliverability/probes.py).

    They spend the same reputation as a real send, so they hold cap slots too.
    """
    from ..deliverability.probes import SENT_STATUSES
    from ..models import SeedTest

    return list(
        db.scalars(
            select(SeedTest.sent_at).where(
                SeedTest.mailbox_id == mailbox_id,
                SeedTest.status.in_(SENT_STATUSES),
                SeedTest.sent_at >= since,
            )
        )
    )


def sent_in_window(db: Session, *, mailbox_id: str, now: datetime) -> int:
    """Sends actually accepted by the provider in the last WINDOW_HOURS.

    Counts status "sent" only: a queued row has not consumed reputation and a
    failed one never left, so neither should hold a slot. Seed tests count.
    """
    if not mailbox_id:
        return 0
    since = now - timedelta(hours=WINDOW_HOURS)
    real = int(
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
    return real + len(_seed_sends_since(db, mailbox_id, since))


def frees_at(db: Session, *, connection: MailboxConnection, now: datetime) -> datetime:
    """When this mailbox next has room: its oldest in-window send ages out.

    ``now`` when it already has room. A send that waits on a full mailbox
    sleeps until this moment rather than retrying every minute -- a thousand
    blocked sends polling once a minute starves the worker and, worse, sat at
    the front of the line while sends that could go waited behind them.
    """
    cap = max(connection.daily_send_cap, 0)
    since = now - timedelta(hours=WINDOW_HOURS)
    rows = list(
        db.scalars(
            select(Message.sent_at)
            .where(
                Message.mailbox_id == connection.id,
                Message.direction == "outbound",
                Message.status == "sent",
                Message.sent_at >= since,
            )
            .order_by(Message.sent_at.asc())
        )
    )
    rows = sorted(rows + _seed_sends_since(db, connection.id, since), key=lambda v: _aware(v))
    if len(rows) < cap or not rows:
        return now
    # Room opens when enough of the oldest sends have left the window that
    # fewer than ``cap`` remain.
    oldest = rows[len(rows) - cap]
    if oldest.tzinfo is None:
        oldest = oldest.replace(tzinfo=UTC)
    return max(now, oldest + timedelta(hours=WINDOW_HOURS))


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
    db: Session,
    *,
    org_id: str,
    now: datetime,
    only: list[str] | None = None,
    exclude: set[str] | None = None,
) -> MailboxConnection | None:
    """The least-utilised mailbox with room left, or None when all are full.

    None does not distinguish "no mailboxes" from "every mailbox is capped";
    callers that need to tell those apart ask ``active_mailboxes`` first, and
    they mean different things: no live connection falls through to the globally
    configured provider, a capped-out pool defers.
    """
    best: MailboxConnection | None = None
    best_load = 1.0
    for connection in eligible_mailboxes(db, org_id=org_id, only=only):
        if exclude and connection.id in exclude:
            continue
        used = sent_in_window(db, mailbox_id=connection.id, now=now)
        if used >= connection.daily_send_cap:
            continue
        load = used / connection.daily_send_cap
        if best is None or load < best_load:
            best, best_load = connection, load
    return best

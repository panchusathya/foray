"""Auto-throttle: seed tests in spam take a mailbox off new conversations, or rest it.

Three states per mailbox, decided only from hard evidence -- seed placements
and provider policy blocks, never reply rate:

* ``ok``        -- normal.
* ``throttled`` -- at least a quarter of recent seed tests not delivered, or
  one policy block. Rotation stops giving it new conversations; the follow-ups
  it already owns still go, inside half its cap. It keeps receiving scheduled
  seed tests, which is how it shows it has recovered (3 fresh clean results).
* ``resting``   -- at least half not delivered, or two policy blocks. Its cap is
  0 for ``REST_DAYS``; its conversations wait (they are never moved to another
  address -- the thread rule). When the rest ends it goes back to ``ok`` with
  warm-up restarted, so it climbs back from 2/day instead of jumping.

Evidence counts only since the mailbox's last state change (and at most a
week back). Without that bound the spam that rested a mailbox would rest it
again the moment it came back, forever.

"Not delivered" is ``spam`` or ``missing``. A tab or an archive is delivered:
the prod bug in README item 37 made 18 archived tests look missing, and a
throttle on top of that would have rested good mailboxes.

The org's ``auto_throttle`` chooses ``off`` / ``watch`` / ``on``. Watch (the
default) computes the same decisions and only shows them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Enrollment, MailboxConnection, Message, Org, SeedTest, record_event
from .signals import classify_bounce

WINDOW = timedelta(days=7)
MIN_RESULTS = 3
THROTTLE_SHARE = 0.25
REST_SHARE = 0.5
THROTTLE_BLOCKS = 1
REST_BLOCKS = 2
REST_DAYS = 3

DELIVERED = ("inbox", "other_tab")
NOT_DELIVERED = ("spam", "missing")
MODES = ("off", "watch", "on")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Evidence:
    results: int = 0
    bad: int = 0
    blocks: int = 0

    @property
    def share(self) -> float:
        return self.bad / self.results if self.results else 0.0


@dataclass(frozen=True)
class Decision:
    mailbox_id: str
    address: str
    from_state: str
    state: str
    reason: str = ""
    rest_until: datetime | None = None
    restart_warmup: bool = False

    @property
    def changed(self) -> bool:
        return self.state != self.from_state


def evidence(db: Session, connection: MailboxConnection, now: datetime) -> Evidence:
    since = now - WINDOW
    changed = _aware(connection.throttle_changed_at)
    if changed is not None and changed > since:
        since = changed
    counts = dict(
        db.execute(
            select(SeedTest.status, func.count(SeedTest.id))
            .where(
                SeedTest.mailbox_id == connection.id,
                SeedTest.sent_at >= since,
                SeedTest.status.in_(DELIVERED + NOT_DELIVERED),
            )
            .group_by(SeedTest.status)
        ).all()
    )
    bounces = db.execute(
        select(Message.reply_text, Message.body)
        .outerjoin(Enrollment, Enrollment.id == Message.enrollment_id)
        .where(
            Message.direction == "inbound",
            Message.reply_intent == "bounce",
            func.coalesce(Message.sent_at, Message.created_at) >= since,
            (Message.mailbox_id == connection.id) | (Enrollment.mailbox_id == connection.id),
        )
    ).all()
    blocks = sum(
        1 for text, body in bounces if classify_bounce(f"{text or ''}\n{body or ''}") == "block"
    )
    return Evidence(
        results=sum(counts.values()),
        bad=sum(counts.get(s, 0) for s in NOT_DELIVERED),
        blocks=blocks,
    )


def _line(ev: Evidence) -> str:
    parts = []
    if ev.results:
        parts.append(f"{ev.bad} of {ev.results} seed tests not delivered")
    if ev.blocks:
        parts.append(f"{ev.blocks} polic{'y' if ev.blocks == 1 else 'ies'} block")
    return ", ".join(parts)


def decide(connection: MailboxConnection, ev: Evidence, now: datetime) -> Decision:
    state = connection.throttle_state or "ok"
    base = {"mailbox_id": connection.id, "address": connection.address, "from_state": state}
    if state == "resting":
        rest_until = _aware(connection.rest_until)
        if rest_until is not None and now < rest_until:
            return Decision(
                **base, state="resting", reason=connection.throttle_reason, rest_until=rest_until
            )
        return Decision(
            **base, state="ok", restart_warmup=True, reason="rest over: ramping back up from 2/day"
        )

    enough = ev.results >= MIN_RESULTS
    severe = ev.blocks >= REST_BLOCKS or (enough and ev.share >= REST_SHARE)
    bad = ev.blocks >= THROTTLE_BLOCKS or (enough and ev.share >= THROTTLE_SHARE)
    if severe:
        return Decision(
            **base, state="resting", reason=_line(ev), rest_until=now + timedelta(days=REST_DAYS)
        )
    if bad:
        if state == "throttled":
            return Decision(**base, state="throttled", reason=connection.throttle_reason)
        return Decision(**base, state="throttled", reason=_line(ev))
    if state == "throttled":
        if enough:
            return Decision(**base, state="ok", reason=f"recovered: {_line(ev)}")
        return Decision(**base, state="throttled", reason=connection.throttle_reason)
    return Decision(**base, state="ok")


def _mailboxes(db: Session, org_id: str) -> list[MailboxConnection]:
    return list(
        db.scalars(
            select(MailboxConnection)
            .where(MailboxConnection.org_id == org_id, MailboxConnection.status == "active")
            .order_by(MailboxConnection.address)
        )
    )


def plan(db: Session, org: Org, now: datetime) -> list[Decision]:
    """The changes the throttle would make now. Empty when it is off."""
    if (org.auto_throttle or "watch") == "off":
        return []
    out = []
    for connection in _mailboxes(db, org.id):
        decision = decide(connection, evidence(db, connection, now), now)
        if decision.changed:
            out.append(decision)
    return out


def apply(db: Session, org: Org, now: datetime) -> list[Decision]:
    """Make the planned changes -- only when the org has it switched on."""
    if (org.auto_throttle or "watch") != "on":
        return []
    applied = []
    for decision in plan(db, org, now):
        connection = db.get(MailboxConnection, decision.mailbox_id)
        set_state(db, connection, decision, now)
        applied.append(decision)
    return applied


def set_state(
    db: Session,
    connection: MailboxConnection,
    decision: Decision,
    now: datetime,
    *,
    actor_id: str | None = None,
) -> None:
    connection.throttle_state = decision.state
    connection.throttle_reason = decision.reason
    connection.throttle_changed_at = now
    connection.rest_until = decision.rest_until if decision.state == "resting" else None
    if decision.restart_warmup:
        connection.warmup_started_at = now
    record_event(
        db,
        org_id=connection.org_id,
        type="deliverability.throttle",
        subject_type="mailbox",
        subject_id=connection.id,
        actor_id=actor_id,
        payload={
            "address": connection.address,
            "from": decision.from_state,
            "to": decision.state,
            "reason": decision.reason,
            "rest_until": decision.rest_until.isoformat() if decision.rest_until else None,
        },
    )
    db.flush()


def is_resting(connection, now: datetime) -> bool:
    if getattr(connection, "throttle_state", "ok") != "resting":
        return False
    until = _aware(getattr(connection, "rest_until", None))
    return until is not None and now < until

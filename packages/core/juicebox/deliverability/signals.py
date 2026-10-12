"""What real outreach traffic says about each sending mailbox.

No provider tells a sender where its mail landed, so these are proxies:

* **Reply rate** of people first emailed 3-17 days ago. Younger than three days
  and nobody has had time to answer -- counting them makes a busy new mailbox
  look like it is in spam (failure 17 in the README). Replies are enrollments
  that ended ``replied``, minus automated out-of-office answers.
* **Weekly reply rate** by first-send week, so a mailbox can be compared with
  its domain in the *same* week rather than across weeks of different weather.
* **Bounce rate** over the last 14 days, and **blocks**: bounces where a
  provider refused on policy or reputation (5.7.x, "detected as spam") rather
  than because the address is dead. A dead address is a list problem; a block
  is the reputation problem this page exists for.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Enrollment, MailboxConnection, Message

RECENT_MIN_AGE = timedelta(days=3)
RECENT_MAX_AGE = timedelta(days=17)
BOUNCE_WINDOW = timedelta(days=14)
WEEKS = 8
_NOT_A_REPLY = {"out_of_office"}

_BLOCK_CODE = re.compile(r"\b5\.7\.\d{1,3}\b")
_ADDRESS_CODE = re.compile(r"\b5\.(?:1\.\d{1,3}|2\.2)\b")
_BLOCK_WORDS = re.compile(
    r"spam|reputation|block(?:ed|list)|blacklist|policy|unauthenticated|suspicious|"
    r"listed|rejected due to|not accepted from this domain",
    re.IGNORECASE,
)
_ADDRESS_WORDS = re.compile(
    r"user unknown|does not exist|no such user|unknown user|mailbox unavailable|"
    r"address rejected|invalid recipient|recipient not found|mailbox (?:is )?full|"
    r"over quota|account (?:has been )?disabled",
    re.IGNORECASE,
)


def classify_bounce(text: str) -> str:
    """``block`` (policy/reputation), ``address`` (dead or full mailbox), or ``other``."""
    text = text or ""
    if _BLOCK_CODE.search(text):
        return "block"
    if _ADDRESS_CODE.search(text) or _ADDRESS_WORDS.search(text):
        return "address"
    if _BLOCK_WORDS.search(text):
        return "block"
    return "other"


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def week_start(value: datetime) -> str:
    value = _aware(value)
    monday = (value - timedelta(days=value.weekday())).date()
    return monday.isoformat()


@dataclass
class MailboxSignals:
    people_recent: int = 0
    replied_recent: int = 0
    sends_14d: int = 0
    bounced_14d: int = 0
    blocks_14d: int = 0
    # week (Monday, ISO) -> [people first emailed that week, of whom replied]
    weekly: dict[str, list[int]] = field(default_factory=dict)

    @property
    def reply_rate_recent(self) -> float | None:
        return self.replied_recent / self.people_recent if self.people_recent else None

    @property
    def bounce_rate_14d(self) -> float | None:
        return self.bounced_14d / self.sends_14d if self.sends_14d else None

    def add(self, other: MailboxSignals) -> None:
        self.people_recent += other.people_recent
        self.replied_recent += other.replied_recent
        self.sends_14d += other.sends_14d
        self.bounced_14d += other.bounced_14d
        self.blocks_14d += other.blocks_14d
        for week, (people, replied) in other.weekly.items():
            slot = self.weekly.setdefault(week, [0, 0])
            slot[0] += people
            slot[1] += replied


def mailbox_signals(db: Session, org_id: str, now: datetime) -> dict[str, MailboxSignals]:
    """mailbox id -> its signals, for every mailbox in the org."""
    out: dict[str, MailboxSignals] = defaultdict(MailboxSignals)
    mailbox_ids = set(
        db.scalars(select(MailboxConnection.id).where(MailboxConnection.org_id == org_id))
    )
    if not mailbox_ids:
        return {}
    horizon = now - timedelta(weeks=WEEKS)

    # First send per conversation, with the mailbox it is pinned to.
    first = (
        select(
            Message.enrollment_id.label("enrollment_id"),
            func.min(Message.sent_at).label("first_at"),
            func.min(Message.mailbox_id).label("first_mailbox"),
        )
        .where(
            Message.org_id == org_id,
            Message.direction == "outbound",
            Message.source == "sequence",
            Message.enrollment_id.is_not(None),
            Message.sent_at.is_not(None),
            Message.status.in_(("sent", "bounced")),
        )
        .group_by(Message.enrollment_id)
        .subquery()
    )
    rows = db.execute(
        select(
            first.c.first_at,
            first.c.first_mailbox,
            Enrollment.mailbox_id,
            Enrollment.status,
            Enrollment.stopped_reason,
        )
        .join(Enrollment, Enrollment.id == first.c.enrollment_id)
        .where(first.c.first_at >= horizon)
    ).all()
    for first_at, first_mailbox, pinned, status, reason in rows:
        mailbox_id = pinned or first_mailbox
        if mailbox_id not in mailbox_ids:
            continue
        first_at = _aware(first_at)
        replied = status == "replied" and reason not in _NOT_A_REPLY
        signals = out[mailbox_id]
        slot = signals.weekly.setdefault(week_start(first_at), [0, 0])
        slot[0] += 1
        slot[1] += int(replied)
        age = now - first_at
        if RECENT_MIN_AGE <= age <= RECENT_MAX_AGE:
            signals.people_recent += 1
            signals.replied_recent += int(replied)

    since = now - BOUNCE_WINDOW
    sends = db.execute(
        select(Message.mailbox_id, Message.status, func.count(Message.id))
        .where(
            Message.org_id == org_id,
            Message.direction == "outbound",
            Message.source == "sequence",
            Message.sent_at >= since,
            Message.status.in_(("sent", "bounced")),
        )
        .group_by(Message.mailbox_id, Message.status)
    ).all()
    for mailbox_id, status, count in sends:
        if mailbox_id not in mailbox_ids:
            continue
        out[mailbox_id].sends_14d += count
        if status == "bounced":
            out[mailbox_id].bounced_14d += count

    bounces = db.execute(
        select(Message.mailbox_id, Enrollment.mailbox_id, Message.reply_text, Message.body)
        .outerjoin(Enrollment, Enrollment.id == Message.enrollment_id)
        .where(
            Message.org_id == org_id,
            Message.direction == "inbound",
            Message.reply_intent == "bounce",
            func.coalesce(Message.sent_at, Message.created_at) >= since,
        )
    ).all()
    for own, pinned, reply_text, body in bounces:
        mailbox_id = own or pinned
        if mailbox_id not in mailbox_ids:
            continue
        if classify_bounce(f"{reply_text or ''}\n{body or ''}") == "block":
            out[mailbox_id].blocks_14d += 1
    return dict(out)


def recent_weeks(now: datetime, count: int = WEEKS) -> list[str]:
    """The last ``count`` week keys, oldest first, ending with this week."""
    this = _aware(now)
    return [week_start(this - timedelta(weeks=n)) for n in range(count - 1, -1, -1)]

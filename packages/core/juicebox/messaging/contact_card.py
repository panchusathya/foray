"""The "New contact information — Add" banner, and when a thread earns one.

Apple will offer to save our name and photo, but only into an iMessage chat
that has already heard from us, and only once a day. So this is a policy
module wrapped around a single vendor call: ``linq.share_contact_card`` knows
how to ask, and everything here is about *when*.

Three rules, each a decision rather than a limit we inherited:

* **They have to have replied first.** Not "we have sent something" — Linq's
  own precondition, which any first outbound satisfies — but an inbound
  message that arrived *after* one of ours. Someone who texted the line once
  and went quiet has not agreed to anything, and asking their phone to file us
  under a name is something you earn by being in a conversation.
* **The candidate line only.** Gated on the number that was texted rather than
  on the channel, so a second Linq line added later for recruiters does not
  silently inherit a candidate-facing identity.
* **iMessage, not merely rich.** ``cards.py`` treats RCS and iMessage alike
  because both render a picture; a contact card is an Apple feature and RCS
  cannot show one, so this one splits them.
* **At most three attempts, a day apart.** Linq documents that a share "may
  not take effect in every chat" and recommends retrying daily, so one attempt
  is not enough; a banner that reappears every day forever is not a retry, it
  is nagging. Three is where those two stop arguing.

State lives on ``thread.record`` under ``_contact_card`` — the same
underscore-prefixed convention ``linkedin_flow`` uses for its flow meta, and
safe there because ``merge_record`` carries unknown top-level keys through
untouched. Not a column: this is bookkeeping for one vendor courtesy, and a
migration is a poor trade for it.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..models import IntakeMessage, IntakeThread, utcnow
from .channels import LINQ
from .linq import SERVICE_IMESSAGE, parse_iso_datetime, share_contact_card
from .threads import INBOUND, OUTBOUND

logger = logging.getLogger(__name__)

#: Where the bookkeeping sits on ``thread.record``.
META_KEY = "_contact_card"

MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=24)


def card_meta(thread: IntakeThread) -> dict[str, Any]:
    """What we have already tried on this thread."""
    return dict((thread.record or {}).get(META_KEY) or {})


def maybe_share(db: Session, *, thread: IntakeThread) -> bool:
    """Offer the card to this chat if it has earned one. Never raises.

    Called after a successful outbound, which is both the natural moment and
    the only legal one: Linq refuses a share into a chat we have never spoken
    in. Returns whether the vendor accepted the share, not whether a banner
    appeared — that is not observable from here.
    """
    try:
        if not _eligible(db, thread=thread):
            return False
        shared = share_contact_card(thread.provider_thread_id)
        now = utcnow().isoformat()
        meta = card_meta(thread)
        remembered: dict[str, Any] = {
            "attempts": int(meta.get("attempts") or 0) + 1,
            "last_attempt_at": now,
        }
        if shared:
            remembered["shared_at"] = now
        _remember(thread, **remembered)
        db.flush()
        return shared
    except Exception as exc:  # noqa: BLE001 - a courtesy; never fail a turn
        logger.warning("contact card share failed on thread %s: %s", thread.id, exc)
        return False


def _eligible(db: Session, *, thread: IntakeThread) -> bool:
    settings = get_settings()
    # The key is checked alongside the switch so a misconfigured deploy spends
    # no attempts: without it the share cannot even be sent, and burning the
    # budget on calls that never left the process would leave those threads
    # permanently ineligible once somebody fixed the config.
    if not settings.linq_contact_card_enabled or not settings.linq_api_key:
        return False
    if (thread.channel or "") != LINQ or not thread.provider_thread_id:
        return False
    if not _is_candidate_line(thread):
        return False
    # iMessage only, and specifically not RCS -- the card is an Apple feature,
    # and Linq refuses the share anywhere else. Checking ``last_service``
    # rather than letting the vendor say no keeps an Android candidate from
    # spending the attempt budget on a call that could never succeed. Blank
    # means we have never been told, which is not evidence of iMessage.
    if (thread.last_service or "") != SERVICE_IMESSAGE:
        return False

    meta = card_meta(thread)
    if int(meta.get("attempts") or 0) >= MAX_ATTEMPTS:
        return False
    last = parse_iso_datetime(meta.get("last_attempt_at"))
    if last is not None and utcnow() - last < RETRY_AFTER:
        return False

    return _has_replied(db, thread=thread)


def _is_candidate_line(thread: IntakeThread) -> bool:
    """Only the number the card was configured for offers it.

    ``LINQ_FROM_NUMBER`` is the candidate line, and the card is provisioned
    against that number at Linq. Gating here on the number that was texted
    means a recruiter-facing line added later starts silent rather than
    introducing itself with the candidate agent's face.
    """
    configured = lookup_key(get_settings().linq_from_number)
    return bool(configured) and configured == lookup_key(thread.to_address)


def _has_replied(db: Session, *, thread: IntakeThread) -> bool:
    """Has this person answered something we sent?

    ``seq`` rather than timestamps: two rows written in one transaction can
    share a ``created_at``, and the question here is precisely which came
    first. Our first outbound is the anchor — an inbound after it is a reply,
    an inbound before it is merely how the conversation opened.
    """
    first_outbound = db.scalar(
        select(func.min(IntakeMessage.seq)).where(
            IntakeMessage.thread_id == thread.id,
            IntakeMessage.direction == OUTBOUND,
        )
    )
    if first_outbound is None:
        return False
    reply = db.scalar(
        select(IntakeMessage.id)
        .where(
            IntakeMessage.thread_id == thread.id,
            IntakeMessage.direction == INBOUND,
            IntakeMessage.seq > first_outbound,
        )
        .limit(1)
    )
    return reply is not None


def _remember(thread: IntakeThread, **kwargs: Any) -> None:
    """Reassign ``record`` so SQLAlchemy notices the JSON mutation."""
    record = dict(thread.record or {})
    meta = dict(record.get(META_KEY) or {})
    meta.update(kwargs)
    record[META_KEY] = meta
    thread.record = record

"""Thread and message persistence.

Everything that writes an ``IntakeThread`` or an ``IntakeMessage`` goes through
here, so the two invariants that matter have one place to live:

* **``seq`` is dense and per-thread.** The transcript is assembled in ``seq``
  order and handed to an extractor whose heuristics read the whole text, so an
  out-of-order message changes what gets extracted. ``created_at`` is not
  enough -- two rows written in one transaction share it.
* **An inbound message is recorded at most once.** The sender's own id is the
  key, exactly as ``Message.provider_native_id`` is for mail. Inbound webhooks
  are retried by every vendor, and a duplicate here is not a duplicate row --
  it is a second extraction and a second reply to a question already answered.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..intake.schema import SCHEMA_VERSION, empty_record
from ..models import IntakeMessage, IntakeThread, record_event, utcnow
from .channels import WEB, DeliveryResult, OutboundMessage
from .linkedin_flow import default_flow

logger = logging.getLogger(__name__)

INBOUND = "inbound"
OUTBOUND = "outbound"

# A group chat's thread. Lives here rather than with the other modes because
# `find_active_thread` has to exclude it, and that query cannot import upwards.
# A group is not one person's conversation: it stays active while we are still
# in the chat, and resuming it for a private message would deliver every later
# reply into the group.
MODE_REFERRAL = "referral"

STATUS_ACTIVE = "active"
STATUS_COMPLETE = "complete"
STATUS_STOPPED = "stopped"

# ``IntakeMessage.status`` on an outbound row carries two different facts, and
# conflating them is what made delivery receipts erase our own questions from
# the transcript:
#
# * **Did we hand it to the carrier?** ``sent`` at write time, ``failed`` when
#   the channel refused or threw. This is the fact every reader actually wants
#   -- it decides whether the message is part of the conversation, whether the
#   intro has already gone out, and whether a nudge was spent.
# * **What did the carrier do next?** ``apply_status_event`` rewrites the same
#   column to ``delivered`` / ``read`` when Linq's receipts arrive, and the
#   deployment subscribes to exactly those events. Nothing ever restores
#   ``sent``.
#
# So ``status == "sent"`` is never the right test: it is true for a few seconds
# and then false forever. Ask ``was_handed_off`` (or the frozenset, in a SQL
# ``in_``) instead.
MSG_SENT = "sent"
MSG_DELIVERED = "delivered"
MSG_READ = "read"
MSG_FAILED = "failed"

#: The statuses that mean the carrier took it. A receipt only ever moves a row
#: further along this list, never back out of it.
HANDED_OFF = frozenset({MSG_SENT, MSG_DELIVERED, MSG_READ})

# How the transcript labels each side. Matches the voice fixtures exactly, so
# the extractor sees the shape its heuristics and its prompt were written for.
_SPEAKER = {INBOUND: "Candidate", OUTBOUND: "Agent"}


def was_handed_off(message: IntakeMessage) -> bool:
    """Did the channel accept this outbound message?

    True for ``sent`` and for every later receipt state. An inbound row is
    never handed off by us -- it arrived -- so callers ask this about outbound
    rows only.
    """
    return (message.status or "") in HANDED_OFF


def open_thread(
    db: Session,
    *,
    org_id: str,
    channel: str = WEB,
    provider: str = "native",
    provider_thread_id: str = "",
    entry_point: str = "web_widget",
    candidate_id: str | None = None,
    from_address: str = "",
    to_address: str = "",
    consent_messaging: bool = False,
    contact_name: str = "",
    contact_email: str = "",
    mode: str = "intake",
    contact_linkedin: str = "",
    flow: str | None = None,
) -> IntakeThread:
    """Start a conversation.

    ``record`` is seeded with ``empty_record()`` rather than ``{}`` so every
    key is present from the first turn: a consumer should never have to work
    out whether a missing key means unasked or unsupported, and ``coverage()``
    can read the row the moment it exists.
    """
    thread = IntakeThread(
        org_id=org_id,
        candidate_id=candidate_id,
        channel=channel,
        provider=provider,
        provider_thread_id=provider_thread_id,
        entry_point=entry_point,
        from_address=from_address,
        to_address=to_address,
        consent_messaging=consent_messaging,
        contact_name=contact_name,
        contact_email=contact_email,
        mode=mode,
        contact_linkedin=contact_linkedin,
        flow=flow or default_flow(channel),
        status=STATUS_ACTIVE,
        record=empty_record(),
        schema_version=SCHEMA_VERSION,
        fields_missing=[],
        low_confidence=[],
        asked_counts={},
        review_needed=True,
    )
    db.add(thread)
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="intake.thread.opened",
        subject_type="intake_thread",
        subject_id=thread.id,
        payload={"channel": channel, "entry_point": entry_point},
    )
    return thread


def find_active_thread(
    db: Session, *, org_id: str, channel: str, from_address: str
) -> IntakeThread | None:
    """The open conversation for this sender on this channel *at this org*.

    Group threads are excluded outright -- see the query. ``org_id`` is not
    optional, and that is the whole point. Two agencies each
    claim their own Linq number; a candidate with an active thread at org A who
    texts org B's number missed the provider-thread lookup and landed back in
    A's thread. The message was written under A, the reply was composed from
    A's transcript and delivered on B's line, and B's recruiters never saw
    that anybody had texted them.
    """
    if not from_address:
        return None
    return db.scalar(
        select(IntakeThread)
        .where(
            IntakeThread.org_id == org_id,
            IntakeThread.channel == channel,
            IntakeThread.from_address == from_address,
            IntakeThread.status == STATUS_ACTIVE,
            # Never a group. A referral thread carries the referrer's own
            # number and stays active while we are still in the chat, so
            # without this a private message from them would resume it -- and
            # its provider_thread_id is the *group*, which means the reply
            # lands in front of the friend they just introduced.
            IntakeThread.mode != MODE_REFERRAL,
        )
        .order_by(IntakeThread.last_inbound_at.desc(), IntakeThread.created_at.desc())
        .limit(1)
    )


def find_by_provider_id(
    db: Session, *, provider: str, provider_thread_id: str
) -> IntakeThread | None:
    if not provider_thread_id:
        return None
    return db.scalar(
        select(IntakeThread).where(
            IntakeThread.provider == provider,
            IntakeThread.provider_thread_id == provider_thread_id,
        )
    )


def stopped_thread_exists(db: Session, *, channel: str, from_address: str) -> bool:
    """Has this sender ever opted out on this channel?

    *Ever*, not most-recently. Looking at the latest thread instead would find
    the conversation asking the question, and a STOP would stop exactly one
    thread while the next inbound message opened a fresh one and started
    asking again.
    """
    if not from_address:
        return False
    return (
        db.scalar(
            select(IntakeThread.id)
            .where(
                IntakeThread.channel == channel,
                IntakeThread.from_address == from_address,
                IntakeThread.status == STATUS_STOPPED,
            )
            .limit(1)
        )
        is not None
    )


def with_send_key(
    db: Session, *, thread: IntakeThread, message: OutboundMessage
) -> OutboundMessage:
    """Attach a stable vendor idempotency key when the caller did not supply one."""
    if message.send_key:
        return message
    seq = _next_seq(db, thread)
    return replace(message, send_key=f"{thread.id}:{seq}")


def _next_seq(db: Session, thread: IntakeThread) -> int:
    highest = db.scalar(
        select(func.max(IntakeMessage.seq)).where(IntakeMessage.thread_id == thread.id)
    )
    return int(highest or 0) + 1


# How many times to re-allocate ``seq`` before giving up. Collisions come in
# ones and twos -- a webhook landing while a card loop is sending -- so a
# small bound is plenty and keeps a genuine constraint bug loud.
_SEQ_RETRIES = 5


def _insert_with_seq(db: Session, thread: IntakeThread, build) -> IntakeMessage:
    """Insert a thread message, re-allocating ``seq`` when a writer takes it.

    ``seq`` is ``max(seq) + 1`` over the thread, which two concurrent writers
    read as the same number; ``uq_intake_message_seq`` then fails the loser.
    In prod that surfaced as the Linq webhook 500-ing under its own retries --
    four concurrent deliveries against a card loop sending four outbound rows
    -- and the candidate's reply arrived three minutes late because the vendor
    kept backing off. Re-reading the high-water mark and trying again keeps the
    unique constraint as the arbiter instead of reaching for a lock.

    ``build`` takes the allocated seq and returns an unsaved row, so each
    attempt gets a fresh object rather than re-adding a rolled-back one.
    """
    for attempt in range(_SEQ_RETRIES):
        row = build(_next_seq(db, thread))
        try:
            with db.begin_nested():
                db.add(row)
                db.flush()
        except IntegrityError:
            with suppress(Exception):
                db.expunge(row)
            if attempt == _SEQ_RETRIES - 1:
                raise
            logger.info(
                "seq collision on thread %s, re-allocating (attempt %d)", thread.id, attempt + 1
            )
            continue
        return row
    raise AssertionError("unreachable")  # pragma: no cover


def record_inbound(
    db: Session,
    *,
    thread: IntakeThread,
    body: str,
    provider_native_id: str = "",
    raw: dict | None = None,
) -> tuple[IntakeMessage, bool]:
    """Store an inbound message. Returns ``(row, created)``.

    ``created=False`` means this exact message has already been recorded and
    the caller must not run another turn on it. That is the whole retry story:
    a vendor redelivering a webhook, or a browser retrying a POST, is a no-op
    rather than a second reply.
    """
    if provider_native_id:
        existing = db.scalar(
            select(IntakeMessage).where(
                IntakeMessage.thread_id == thread.id,
                IntakeMessage.provider_native_id == provider_native_id,
            )
        )
        if existing is not None:
            return existing, False

    def build(seq: int) -> IntakeMessage:
        return IntakeMessage(
            org_id=thread.org_id,
            thread_id=thread.id,
            seq=seq,
            direction=INBOUND,
            body=body or "",
            provider_native_id=provider_native_id,
            status="received",
            raw=raw or {},
        )

    try:
        message = _insert_with_seq(db, thread, build)
    except IntegrityError:
        # Two deliveries of the same webhook raced. The winner's row is the
        # answer; returning it as not-created keeps the turn from running twice.
        if not provider_native_id:
            raise
        found = db.scalar(
            select(IntakeMessage).where(
                IntakeMessage.thread_id == thread.id,
                IntakeMessage.provider_native_id == provider_native_id,
            )
        )
        if found is None:
            raise
        return found, False

    thread.inbound_count = (thread.inbound_count or 0) + 1
    thread.last_inbound_at = utcnow()
    db.flush()
    return message, True


def record_outbound(
    db: Session,
    *,
    thread: IntakeThread,
    message: OutboundMessage,
    result: DeliveryResult,
    origin: str = "reply",
) -> IntakeMessage:
    """Store what we said, whether or not the channel managed to deliver it.

    A failed send is still a row: without one there is no evidence a question
    was attempted, and the next turn would compose the same question again.

    ``origin`` distinguishes an answer (``reply``) from a message nobody asked
    for (``proactive``), a nudge (``nudge``), and protocol traffic
    (``system``): the nudge budget in ``cadence.py`` counts nudge rows — and
    only those — so what the budget charges for is exactly what was sent.

    ``status`` here is the handoff fact: ``sent`` or ``failed``. Receipts
    overwrite it later with ``delivered`` / ``read``, which is why readers use
    ``was_handed_off`` rather than comparing against ``sent``.
    """
    sent = result.status == MSG_SENT
    row = _insert_with_seq(
        db,
        thread,
        lambda seq: IntakeMessage(
            org_id=thread.org_id,
            thread_id=thread.id,
            seq=seq,
            direction=OUTBOUND,
            body=message.body,
            provider_native_id=result.provider_native_id,
            asked_field=message.asked_field,
            status=result.status,
            error=result.error,
            origin=origin,
            sent_at=utcnow() if sent else None,
        ),
    )

    if sent:
        thread.outbound_count = (thread.outbound_count or 0) + 1
        thread.last_outbound_at = utcnow()
        if message.asked_field:
            thread.last_field_asked = message.asked_field
            counts = dict(thread.asked_counts or {})
            counts[message.asked_field] = int(counts.get(message.asked_field, 0)) + 1
            # Reassigned rather than mutated: SQLAlchemy does not track
            # in-place changes to a JSON dict, so a mutation here would be
            # dropped and the re-ask cap would never bite.
            thread.asked_counts = counts
    db.flush()
    return row


def apply_status_event(
    db: Session,
    *,
    provider_native_id: str,
    event_type: str,
    delivered_at: datetime | None = None,
    read_at: datetime | None = None,
    error: str = "",
) -> IntakeMessage | None:
    """Stamp delivery/read/failure onto an outbound row. Idempotent.

    ``read`` implies ``delivered``. Timestamps are only set, never cleared, so
    a delayed ``message.delivered`` after ``message.read`` is a no-op on
    ``read_at`` and fills ``delivered_at`` if it was missing.

    **This is the writer that moves ``status`` past ``sent``.** Nothing puts it
    back, so a reader asking "did this go out?" must ask ``was_handed_off`` --
    comparing against ``sent`` means the answer flips to no the moment a
    receipt lands, which erased our own questions from the transcript and
    re-sent the LinkedIn intro on every reply.
    """
    if not provider_native_id:
        return None
    row = db.scalar(
        select(IntakeMessage).where(IntakeMessage.provider_native_id == provider_native_id)
    )
    if row is None:
        return None
    if delivered_at and row.delivered_at is None:
        row.delivered_at = delivered_at
    if read_at and row.read_at is None:
        row.read_at = read_at
        if row.delivered_at is None:
            row.delivered_at = read_at
    if event_type == "message.failed":
        row.status = MSG_FAILED
        if error:
            row.error = error[:400]
    elif event_type == "message.read":
        if row.status != MSG_FAILED:
            row.status = MSG_READ
    elif event_type == "message.delivered":
        if row.status not in {MSG_READ, MSG_FAILED}:
            row.status = MSG_DELIVERED
    db.flush()
    return row


def messages_of(db: Session, thread: IntakeThread) -> list[IntakeMessage]:
    return list(
        db.scalars(
            select(IntakeMessage)
            .where(IntakeMessage.thread_id == thread.id)
            .order_by(IntakeMessage.seq)
        ).all()
    )


def transcript_of(db: Session, thread: IntakeThread, *, candidate_only: bool = False) -> str:
    """The thread as the transcript the extractor expects.

    Same ``Agent:`` / ``Candidate:`` framing as a call, because that is the
    shape ``intake.extract`` was written and tested against. A thread really is
    just a slower transcript, and rendering it as one is what lets the
    extractor be reused rather than reimplemented.

    Outbound messages the channel never accepted are left out: the candidate
    never saw them, so treating them as part of the conversation would have the
    extractor read an answer as a response to a question that was never asked.
    ``was_handed_off``, not ``status == "sent"`` -- a delivery receipt used to
    drop the question out of the transcript underneath its own answer, leaving
    the extractor a bare ``Candidate: 260`` with nothing to bind it to.

    ``candidate_only`` drops our side entirely, and exists for the heuristic
    extractor. The model needs the questions to know what an answer refers to;
    a regex has no such notion and simply matches the whole text, so it reads
    our own greeting as though the candidate had said it. That is not a
    cosmetic problem -- see ``conversation._reread``.
    """
    lines: list[str] = []
    for message in messages_of(db, thread):
        if message.direction == OUTBOUND and (candidate_only or not was_handed_off(message)):
            continue
        body = (message.body or "").strip()
        if not body:
            continue
        lines.append(f"{_SPEAKER[message.direction]}: {body}")
    return "\n".join(lines)


def close_thread(
    db: Session, *, thread: IntakeThread, status: str, reason: str = ""
) -> IntakeThread:
    """Terminal state, recorded once.

    Re-closing is a no-op rather than an error: a STOP arriving twice, or a
    suppression check running after the wrap-up message, must not rewrite
    ``completed_at`` or emit a second event.
    """
    if thread.status == status:
        return thread
    thread.status = status
    thread.completed_at = utcnow()
    if reason:
        thread.review_reason = reason[:400]
    record_event(
        db,
        org_id=thread.org_id,
        type=f"intake.thread.{status}",
        subject_type="intake_thread",
        subject_id=thread.id,
        payload={"reason": reason, "channel": thread.channel},
    )
    db.flush()
    return thread

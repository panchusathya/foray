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

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..intake.schema import SCHEMA_VERSION, empty_record
from ..models import IntakeMessage, IntakeThread, record_event, utcnow
from .channels import WEB, DeliveryResult, OutboundMessage

logger = logging.getLogger(__name__)

INBOUND = "inbound"
OUTBOUND = "outbound"

STATUS_ACTIVE = "active"
STATUS_COMPLETE = "complete"
STATUS_STOPPED = "stopped"

# How the transcript labels each side. Matches the voice fixtures exactly, so
# the extractor sees the shape its heuristics and its prompt were written for.
_SPEAKER = {INBOUND: "Candidate", OUTBOUND: "Agent"}


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


def find_active_thread(db: Session, *, channel: str, from_address: str) -> IntakeThread | None:
    """The open conversation for this sender on this channel, if any."""
    if not from_address:
        return None
    return db.scalar(
        select(IntakeThread)
        .where(
            IntakeThread.channel == channel,
            IntakeThread.from_address == from_address,
            IntakeThread.status == STATUS_ACTIVE,
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
    return OutboundMessage(
        body=message.body,
        asked_field=message.asked_field,
        send_key=f"{thread.id}:{seq}",
    )


def _next_seq(db: Session, thread: IntakeThread) -> int:
    highest = db.scalar(
        select(func.max(IntakeMessage.seq)).where(IntakeMessage.thread_id == thread.id)
    )
    return int(highest or 0) + 1


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

    message = IntakeMessage(
        org_id=thread.org_id,
        thread_id=thread.id,
        seq=_next_seq(db, thread),
        direction=INBOUND,
        body=body or "",
        provider_native_id=provider_native_id,
        status="received",
        raw=raw or {},
    )
    try:
        with db.begin_nested():
            db.add(message)
            db.flush()
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
) -> IntakeMessage:
    """Store what we said, whether or not the channel managed to deliver it.

    A failed send is still a row: without one there is no evidence a question
    was attempted, and the next turn would compose the same question again.
    """
    sent = result.status == "sent"
    row = IntakeMessage(
        org_id=thread.org_id,
        thread_id=thread.id,
        seq=_next_seq(db, thread),
        direction=OUTBOUND,
        body=message.body,
        provider_native_id=result.provider_native_id,
        asked_field=message.asked_field,
        status=result.status,
        error=result.error,
        sent_at=utcnow() if sent else None,
    )
    db.add(row)

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

    Undelivered outbound messages are left out: the candidate never saw them,
    so treating them as part of the conversation would have the extractor read
    an answer as a response to a question that was never asked.

    ``candidate_only`` drops our side entirely, and exists for the heuristic
    extractor. The model needs the questions to know what an answer refers to;
    a regex has no such notion and simply matches the whole text, so it reads
    our own greeting as though the candidate had said it. That is not a
    cosmetic problem -- see ``conversation._reread``.
    """
    lines: list[str] = []
    for message in messages_of(db, thread):
        if message.direction == OUTBOUND and (candidate_only or message.status != "sent"):
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

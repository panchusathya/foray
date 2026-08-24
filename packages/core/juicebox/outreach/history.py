"""Per-candidate contact history: aggregates and the thread-grouped timeline.

Computed per request over a page of candidate ids rather than denormalised
onto candidates — a stored last_emailed_at would need writing from four call
sites (sequencer send, record_reply, both thread-sync ingest paths) plus a
backfill, and ix_messages_candidate_sent makes the grouped query one index
scan. ``CASE`` rather than ``FILTER`` so SQLite and Postgres both run it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..models import Event, IntakeCall, IntakeMessage, IntakeThread, Message


@dataclass
class HistoryStats:
    first_contacted_at: datetime | None = None
    last_emailed_at: datetime | None = None
    last_reply_at: datetime | None = None
    outbound_count: int = 0
    inbound_count: int = 0


@dataclass
class Thread:
    conversation_id: str
    subject: str
    last_at: datetime | None
    messages: list[Message] = field(default_factory=list)


@dataclass
class IntakeTimelineMessage:
    id: str
    direction: str
    body: str
    asked_field: str
    status: str
    created_at: datetime
    sent_at: datetime | None


@dataclass
class IntakeTimelineThread:
    thread_id: str
    channel: str
    status: str
    last_at: datetime | None
    messages: list[IntakeTimelineMessage] = field(default_factory=list)


def _when(column):
    return func.coalesce(column, Message.created_at)


def contact_rollup_subquery(org_id: str):
    """The same aggregate as ``candidate_history_stats``, shaped as a joinable
    subquery so the CRM grid can *sort and filter* on contact recency.

    ``candidate_history_stats`` decorates a page whose ids are already known --
    it cannot answer "order every candidate by last contact". This can, at the
    cost of a grouped scan of the org's messages, which is bounded by people
    who actually have mail rather than by the sourced pool.

    One definition, two callers: a divergence here would mean the grid sorts by
    one number and the row displays another.
    """
    outbound = Message.direction == "outbound"
    inbound = Message.direction == "inbound"
    return (
        select(
            Message.candidate_id.label("candidate_id"),
            func.min(case((outbound, _when(Message.sent_at)))).label("first_contacted_at"),
            func.max(case((outbound, _when(Message.sent_at)))).label("last_emailed_at"),
            func.max(case((inbound, _when(Message.sent_at)))).label("last_reply_at"),
            func.sum(case((outbound, 1), else_=0)).label("outbound_count"),
            func.sum(case((inbound, 1), else_=0)).label("inbound_count"),
        )
        .where(
            Message.org_id == org_id,
            # Queued rows are intentions, failed rows never reached anyone.
            Message.status.in_(("sent", "received")),
        )
        .group_by(Message.candidate_id)
        .subquery()
    )


def candidate_history_stats(
    db: Session, *, org_id: str, candidate_ids: list[str]
) -> dict[str, HistoryStats]:
    """One grouped query for a whole page of candidates — never per-row."""
    if not candidate_ids:
        return {}
    outbound = Message.direction == "outbound"
    inbound = Message.direction == "inbound"
    rows = db.execute(
        select(
            Message.candidate_id,
            func.min(case((outbound, _when(Message.sent_at)))).label("first_contacted_at"),
            func.max(case((outbound, _when(Message.sent_at)))).label("last_emailed_at"),
            func.max(case((inbound, _when(Message.sent_at)))).label("last_reply_at"),
            func.sum(case((outbound, 1), else_=0)).label("outbound_count"),
            func.sum(case((inbound, 1), else_=0)).label("inbound_count"),
        )
        .where(
            Message.org_id == org_id,
            Message.candidate_id.in_(candidate_ids),
            # Queued rows are intentions, failed rows never reached anyone.
            Message.status.in_(("sent", "received")),
        )
        .group_by(Message.candidate_id)
    ).all()
    return {
        row.candidate_id: HistoryStats(
            first_contacted_at=row.first_contacted_at,
            last_emailed_at=row.last_emailed_at,
            last_reply_at=row.last_reply_at,
            outbound_count=int(row.outbound_count or 0),
            inbound_count=int(row.inbound_count or 0),
        )
        for row in rows
    }


def candidate_timeline(
    db: Session, *, org_id: str, candidate_id: str, limit: int = 200
) -> tuple[HistoryStats, list[Thread]]:
    """The stats header plus messages grouped into conversation threads,
    newest thread first, messages oldest-first within a thread."""
    stats = candidate_history_stats(
        db, org_id=org_id, candidate_ids=[candidate_id]
    ).get(candidate_id, HistoryStats())

    messages = db.scalars(
        select(Message)
        .where(
            Message.org_id == org_id,
            Message.candidate_id == candidate_id,
            Message.status.in_(("sent", "received", "failed")),
        )
        .order_by(func.coalesce(Message.sent_at, Message.created_at).desc())
        .limit(limit)
    ).all()

    threads: dict[str, Thread] = {}
    order: list[str] = []
    for message in messages:  # newest first, so thread order falls out
        key = message.conversation_id or f"solo:{message.id}"
        if key not in threads:
            threads[key] = Thread(
                conversation_id=message.conversation_id or "",
                subject=message.subject or "(no subject)",
                last_at=message.sent_at or message.created_at,
            )
            order.append(key)
        threads[key].messages.append(message)
    for thread in threads.values():
        thread.messages.sort(key=lambda m: (m.sent_at or m.created_at))
    return stats, [threads[key] for key in order]


def intake_events_for_candidate(
    db: Session, *, org_id: str, candidate_id: str, limit: int = 50
) -> list[Event]:
    """Intake events for calls and messaging threads attributed to this person.

    ``record_event`` writes against the call or thread id. The rendered
    timeline finds both so a text intake is not invisible next to a call.
    """
    call_ids = select(IntakeCall.id).where(
        IntakeCall.org_id == org_id,
        IntakeCall.candidate_id == candidate_id,
    )
    thread_ids = select(IntakeThread.id).where(
        IntakeThread.org_id == org_id,
        IntakeThread.candidate_id == candidate_id,
    )
    return list(
        db.scalars(
            select(Event)
            .where(
                Event.org_id == org_id,
                Event.type.like("intake.%"),
                Event.subject_id.in_(call_ids.union(thread_ids)),
            )
            .order_by(Event.created_at.desc())
            .limit(limit)
        ).all()
    )


def intake_threads_for_candidate(
    db: Session, *, org_id: str, candidate_id: str, limit: int = 20
) -> list[IntakeTimelineThread]:
    """Attributed messaging conversations with their messages, oldest first."""
    threads = list(
        db.scalars(
            select(IntakeThread)
            .where(
                IntakeThread.org_id == org_id,
                IntakeThread.candidate_id == candidate_id,
            )
            .order_by(IntakeThread.created_at.desc())
            .limit(limit)
        ).all()
    )
    if not threads:
        return []

    thread_ids = [thread.id for thread in threads]
    messages = list(
        db.scalars(
            select(IntakeMessage)
            .where(IntakeMessage.thread_id.in_(thread_ids))
            .order_by(IntakeMessage.thread_id, IntakeMessage.seq)
        ).all()
    )
    by_thread: dict[str, list[IntakeTimelineMessage]] = {thread.id: [] for thread in threads}
    for message in messages:
        by_thread.setdefault(message.thread_id, []).append(
            IntakeTimelineMessage(
                id=message.id,
                direction=message.direction,
                body=message.body or "",
                asked_field=message.asked_field or "",
                status=message.status or "",
                created_at=message.created_at,
                sent_at=message.sent_at,
            )
        )

    result: list[IntakeTimelineThread] = []
    for thread in threads:
        items = by_thread.get(thread.id) or []
        last_at = thread.last_inbound_at or thread.last_outbound_at or thread.created_at
        if items:
            last_at = items[-1].sent_at or items[-1].created_at
        result.append(
            IntakeTimelineThread(
                thread_id=thread.id,
                channel=thread.channel,
                status=thread.status,
                last_at=last_at,
                messages=items,
            )
        )
    return result

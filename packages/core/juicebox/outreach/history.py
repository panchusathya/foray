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

from ..models import Message


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


def _when(column):
    return func.coalesce(column, Message.created_at)


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

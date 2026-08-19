"""Background worker: claim jobs, run them, schedule the next tick.

    juicebox work          # loop
    juicebox work --once   # drain the queue and exit (what `make worker` runs)
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .jobs import (
    KIND_AGENT_TICK,
    KIND_MAILBOX_BACKFILL,
    KIND_MAILBOX_SYNC,
    KIND_OUTREACH_SEND,
    KIND_OUTREACH_TICK,
    KIND_REPLY_DRAFT,
    claim,
    enqueue,
    has_open_job,
    mark_failed,
    mark_succeeded,
    recover_stale_locks,
)
from .models import Enrollment, Job, MailboxConnection, Org, Sequence, utcnow

logger = logging.getLogger(__name__)


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def ensure_ticks(db: Session, *, now=None) -> int:
    """Make sure every org has a queued outreach tick and agent tick."""
    now = now or utcnow()
    created = 0
    for org in db.scalars(select(Org)).all():
        if not has_open_job(db, org_id=org.id, kind=KIND_OUTREACH_TICK):
            enqueue(
                db,
                org_id=org.id,
                kind=KIND_OUTREACH_TICK,
                idempotency_key=f"{KIND_OUTREACH_TICK}:{org.id}:{int(now.timestamp())}",
                payload={"org_id": org.id},
                run_at=now,
            )
            created += 1
        if not has_open_job(db, org_id=org.id, kind=KIND_AGENT_TICK):
            enqueue(
                db,
                org_id=org.id,
                kind=KIND_AGENT_TICK,
                idempotency_key=f"{KIND_AGENT_TICK}:{org.id}:{int(now.timestamp())}",
                payload={"org_id": org.id},
                run_at=now,
            )
            created += 1
        if _org_has_active_mailbox(db, org.id) and not has_open_job(
            db, org_id=org.id, kind=KIND_MAILBOX_SYNC
        ):
            enqueue(
                db,
                org_id=org.id,
                kind=KIND_MAILBOX_SYNC,
                idempotency_key=f"{KIND_MAILBOX_SYNC}:{org.id}:{int(now.timestamp())}",
                payload={"org_id": org.id},
                run_at=now,
            )
            created += 1
    return created


def _org_has_active_mailbox(db: Session, org_id: str) -> bool:
    return (
        db.scalar(
            select(MailboxConnection.id).where(
                MailboxConnection.org_id == org_id,
                MailboxConnection.status == "active",
            )
        )
        is not None
    )


def _schedule_next_tick(db: Session, *, org_id: str, kind: str, now) -> None:
    settings = get_settings()
    run_at = now + timedelta(seconds=max(settings.worker_tick_seconds, 1))
    enqueue(
        db,
        org_id=org_id,
        kind=kind,
        idempotency_key=f"{kind}:{org_id}:{int(run_at.timestamp())}",
        payload={"org_id": org_id},
        run_at=run_at,
    )


def _handle_outreach_tick(db: Session, job: Job, now) -> dict[str, Any]:
    from .outreach.sequencer import due_enrollments

    org_id = job.payload.get("org_id") or job.org_id
    queued = 0
    for enrollment in due_enrollments(db, org_id=org_id, now=now):
        sequence = enrollment.sequence
        steps = sorted(sequence.steps, key=lambda s: s.step_order)
        if enrollment.current_step >= len(steps):
            continue
        step = steps[enrollment.current_step]
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_OUTREACH_SEND,
            idempotency_key=f"{KIND_OUTREACH_SEND}:{enrollment.id}:{step.id}",
            payload={
                "org_id": org_id,
                "enrollment_id": enrollment.id,
                "step_id": step.id,
            },
            run_at=now,
        )
        queued += 1
    _schedule_next_tick(db, org_id=org_id, kind=KIND_OUTREACH_TICK, now=now)
    return {"queued_sends": queued}


def _handle_outreach_send(db: Session, job: Job, now) -> dict[str, Any]:
    from sqlalchemy.orm import selectinload

    from .outreach.sequencer import process_enrollment

    enrollment_id = job.payload.get("enrollment_id")
    enrollment = db.scalar(
        select(Enrollment)
        .where(Enrollment.id == enrollment_id)
        .options(selectinload(Enrollment.sequence).selectinload(Sequence.steps))
    )
    if enrollment is None:
        return {"skipped": "missing_enrollment"}
    status, _message = process_enrollment(
        db,
        enrollment,
        now=now,
        dry_run=False,
        step_id=job.payload.get("step_id"),
    )
    if status == "deferred":
        return {"defer": True, "status": status}
    return {"status": status}


def _handle_mailbox_sync(db: Session, job: Job, now) -> dict[str, Any]:
    from .integrations.sync import sync_org_mailboxes

    org_id = job.payload.get("org_id") or job.org_id
    results = sync_org_mailboxes(db, org_id=org_id, now=now)
    # Reschedule only while a mailbox is connected; ensure_ticks restarts the
    # loop when one is connected later.
    if _org_has_active_mailbox(db, org_id):
        settings = get_settings()
        run_at = now + timedelta(seconds=max(settings.mailbox_sync_seconds, 30))
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_MAILBOX_SYNC,
            idempotency_key=f"{KIND_MAILBOX_SYNC}:{org_id}:{int(run_at.timestamp())}",
            payload={"org_id": org_id},
            run_at=run_at,
        )
    return {
        "mailboxes": len(results),
        "matched": sum(s.matched for s in results.values()),
        "seen": sum(s.seen for s in results.values()),
    }


def _handle_reply_draft(db: Session, job: Job, now) -> dict[str, Any]:
    from .integrations.reply_drafting import create_reply_draft
    from .models import Message

    inbound = db.get(Message, job.payload.get("inbound_message_id"))
    connection = db.get(MailboxConnection, job.payload.get("mailbox_id"))
    if inbound is None or connection is None:
        return {"skipped": "missing_message_or_mailbox"}
    if connection.status != "active":
        return {"skipped": f"mailbox_{connection.status}"}
    draft = create_reply_draft(db, inbound=inbound, connection=connection, now=now)
    if draft is None:
        return {"skipped": "no_draft_for_intent"}
    return {"draft_id": draft.id, "generated_by": draft.generated_by}


def _handle_agent_tick(db: Session, job: Job, now) -> dict[str, Any]:
    from .agents.sourcing import run_due_agents

    org_id = job.payload.get("org_id") or job.org_id
    runs = run_due_agents(db, org_id=org_id, now=now)
    _schedule_next_tick(db, org_id=org_id, kind=KIND_AGENT_TICK, now=now)
    return {
        "agents": len(runs),
        "added": sum(run.added for run in runs),
        "enrolled": sum(run.enrolled for run in runs),
    }


def _handle_mailbox_backfill(db: Session, job: Job, now) -> dict[str, Any]:
    """One batch of the historical import; re-enqueues itself until done."""
    from .integrations.backfill import backfill_mailbox_history
    from .models import MailboxConnection

    connection = db.get(MailboxConnection, job.payload.get("mailbox_id", ""))
    if connection is None or connection.status != "active":
        return {"skipped": "mailbox gone or inactive"}
    offset = int(job.payload.get("offset") or 0)
    result = backfill_mailbox_history(db, connection, offset=offset, now=now)
    next_offset = result.get("next_offset")
    if next_offset is not None:
        enqueue(
            db,
            org_id=job.org_id,
            kind=KIND_MAILBOX_BACKFILL,
            idempotency_key=f"{KIND_MAILBOX_BACKFILL}:{connection.id}:{next_offset}",
            payload={"org_id": job.org_id, "mailbox_id": connection.id, "offset": next_offset},
            run_at=now + timedelta(seconds=30),
        )
    return result


HANDLERS = {
    KIND_OUTREACH_TICK: _handle_outreach_tick,
    KIND_OUTREACH_SEND: _handle_outreach_send,
    KIND_AGENT_TICK: _handle_agent_tick,
    KIND_MAILBOX_SYNC: _handle_mailbox_sync,
    KIND_MAILBOX_BACKFILL: _handle_mailbox_backfill,
    KIND_REPLY_DRAFT: _handle_reply_draft,
}


def process_job(db: Session, job: Job, *, now=None) -> None:
    now = now or utcnow()
    handler = HANDLERS.get(job.kind)
    if handler is None:
        mark_failed(job, f"unknown kind {job.kind}", now=now)
        return
    try:
        result = handler(db, job, now) or {}
    except Exception as exc:
        logger.exception("job %s (%s) failed", job.id, job.kind)
        mark_failed(job, str(exc), now=now)
        return
    if result.get("defer"):
        settings = get_settings()
        job.status = "queued"
        job.locked_at = None
        job.locked_by = ""
        job.run_at = now + timedelta(seconds=max(settings.worker_tick_seconds, 30))
        return
    mark_succeeded(job, now=now)


def process_batch(db: Session, *, worker: str | None = None, now=None) -> int:
    now = now or utcnow()
    recover_stale_locks(db, now=now)
    jobs = claim(db, worker_id=worker or worker_id(), now=now)
    for job in jobs:
        process_job(db, job, now=now)
    if jobs:
        db.flush()
    return len(jobs)


def drain(db: Session, *, worker: str | None = None, now=None) -> int:
    """Run until the queue is idle. Used by ``juicebox work --once``."""
    now = now or utcnow()
    ensure_ticks(db, now=now)
    total = 0
    while True:
        n = process_batch(db, worker=worker, now=now)
        if n == 0:
            break
        total += n
    return total


def run_loop(*, poll_seconds: float | None = None) -> None:
    from .db import session_scope

    settings = get_settings()
    delay = settings.worker_poll_seconds if poll_seconds is None else poll_seconds
    wid = worker_id()
    logger.info("worker %s polling every %.1fs", wid, delay)
    while True:
        with session_scope() as db:
            ensure_ticks(db)
            n = process_batch(db, worker=wid)
        if n == 0:
            time.sleep(max(delay, 0.2))

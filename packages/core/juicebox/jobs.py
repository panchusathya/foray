"""Durable job queue.

Postgres workers claim with ``FOR UPDATE SKIP LOCKED``. SQLite has no skip-locked,
so tests (and ``make worker`` on the demo file) just take the next queued row —
one process at a time is the contract there.

Enqueue is idempotent on ``(org_id, idempotency_key)``. Send jobs keep that key
forever so a step cannot be enqueued twice; ticks use a timestamped key so the
next tick can be scheduled after the current one succeeds.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import get_settings
from .models import Job, utcnow

logger = logging.getLogger(__name__)

KIND_OUTREACH_TICK = "outreach.tick"
KIND_OUTREACH_SEND = "outreach.send"
KIND_AGENT_TICK = "agent.tick"
# mailbox.sync is the per-org dispatcher; mailbox.sync.one does the work for
# a single mailbox. Splitting them keeps one slow or backlogged mailbox from
# eating a whole tick and starving the others -- with six mailboxes polling two
# folders each, plus a content fetch per new message, one job per org does not
# fit in a 120s tick.
KIND_MAILBOX_SYNC = "mailbox.sync"
KIND_MAILBOX_SYNC_ONE = "mailbox.sync.one"
KIND_MAILBOX_BACKFILL = "mailbox.backfill"
KIND_REPLY_DRAFT = "reply.draft"

OPEN_STATUSES = ("queued", "running")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def enqueue(
    db: Session,
    *,
    org_id: str,
    kind: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    run_at: datetime | None = None,
    max_attempts: int | None = None,
) -> Job:
    """Insert a job, or return the existing row with this idempotency key."""
    existing = db.scalar(
        select(Job).where(Job.org_id == org_id, Job.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing

    settings = get_settings()
    job = Job(
        org_id=org_id,
        kind=kind,
        payload=payload or {},
        idempotency_key=idempotency_key,
        run_at=run_at or utcnow(),
        max_attempts=max_attempts or settings.worker_max_attempts,
    )
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        found = db.scalar(
            select(Job).where(Job.org_id == org_id, Job.idempotency_key == idempotency_key)
        )
        if found is None:
            raise
        return found
    return job


def has_open_job(db: Session, *, org_id: str, kind: str) -> bool:
    return (
        db.scalar(
            select(Job.id).where(
                Job.org_id == org_id,
                Job.kind == kind,
                Job.status.in_(OPEN_STATUSES),
            )
        )
        is not None
    )


def recover_stale_locks(db: Session, *, now: datetime | None = None) -> int:
    """Re-queue jobs whose worker died while holding the lock."""
    settings = get_settings()
    now = now or utcnow()
    cutoff = now - timedelta(seconds=max(settings.worker_lock_timeout_seconds, 30))
    recovered = 0
    rows = db.scalars(
        select(Job).where(Job.status == "running", Job.locked_at.is_not(None))
    ).all()
    for job in rows:
        locked_at = _aware(job.locked_at)
        if locked_at is None or locked_at > cutoff:
            continue
        job.status = "queued"
        job.locked_at = None
        job.locked_by = ""
        job.last_error = "lock timed out"
        recovered += 1
    if recovered:
        db.flush()
    return recovered


def claim(
    db: Session,
    *,
    worker_id: str,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[Job]:
    settings = get_settings()
    now = now or utcnow()
    batch = limit if limit is not None else settings.worker_batch_size
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_at <= now)
        .order_by(Job.run_at, Job.created_at)
        .limit(max(batch, 1))
    )
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    jobs = list(db.scalars(stmt).all())
    for job in jobs:
        job.status = "running"
        job.locked_at = now
        job.locked_by = worker_id
    if jobs:
        db.flush()
    return jobs


def mark_succeeded(job: Job, *, now: datetime | None = None) -> None:
    now = now or utcnow()
    job.status = "succeeded"
    job.finished_at = now
    job.locked_at = None
    job.locked_by = ""
    job.last_error = ""


def mark_failed(job: Job, error: str, *, now: datetime | None = None) -> None:
    """Retry with backoff, or park the job once attempts are exhausted."""
    now = now or utcnow()
    job.attempts += 1
    job.last_error = (error or "failed")[:2000]
    job.locked_at = None
    job.locked_by = ""
    if job.attempts >= job.max_attempts:
        job.status = "failed"
        job.finished_at = now
        return
    delay = min(2 ** max(job.attempts, 1), 300)
    job.status = "queued"
    job.run_at = now + timedelta(seconds=delay)

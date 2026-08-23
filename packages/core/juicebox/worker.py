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
    KIND_CRM_DISCOVERY,
    KIND_CRM_DISCOVERY_IMPORT,
    KIND_CRM_DISCOVERY_ONE,
    KIND_INTAKE_EXTRACT,
    KIND_INTAKE_TURN,
    KIND_JOBS_MATCH,
    KIND_JOBS_MATCH_ONE,
    KIND_JOBS_PARSE,
    KIND_JOBS_SCRAPE,
    KIND_JOBS_SCRAPE_ONE,
    KIND_MAILBOX_BACKFILL,
    KIND_MAILBOX_SYNC,
    KIND_MAILBOX_SYNC_ONE,
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
        if not has_open_job(db, org_id=org.id, kind=KIND_JOBS_MATCH):
            enqueue(
                db,
                org_id=org.id,
                kind=KIND_JOBS_MATCH,
                idempotency_key=f"{KIND_JOBS_MATCH}:{org.id}:{int(now.timestamp())}",
                payload={"org_id": org.id},
                run_at=now,
            )
            created += 1
        if not has_open_job(db, org_id=org.id, kind=KIND_JOBS_SCRAPE):
            enqueue(
                db,
                org_id=org.id,
                kind=KIND_JOBS_SCRAPE,
                idempotency_key=f"{KIND_JOBS_SCRAPE}:{org.id}:{int(now.timestamp())}",
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
    """Fan one sync round out to one job per mailbox, then reschedule itself.

    The per-mailbox jobs are spread across the interval rather than all fired
    at once: six mailboxes hitting two providers in the same second is how you
    find a rate limit, and nothing here is latency-critical to the second.
    """
    org_id = job.payload.get("org_id") or job.org_id
    settings = get_settings()
    interval = max(settings.mailbox_sync_seconds, 30)

    mailboxes = db.scalars(
        select(MailboxConnection)
        .where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.status == "active",
        )
        .order_by(MailboxConnection.id)
    ).all()
    stagger = interval // max(len(mailboxes), 1)
    queued = 0
    for index, connection in enumerate(mailboxes):
        run_at = now + timedelta(seconds=stagger * index)
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_MAILBOX_SYNC_ONE,
            idempotency_key=(
                f"{KIND_MAILBOX_SYNC_ONE}:{connection.id}:{int(run_at.timestamp())}"
            ),
            payload={"org_id": org_id, "mailbox_id": connection.id},
            run_at=run_at,
        )
        queued += 1

    # Reschedule only while a mailbox is connected; ensure_ticks restarts the
    # loop when one is connected later.
    if mailboxes:
        run_at = now + timedelta(seconds=interval)
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_MAILBOX_SYNC,
            idempotency_key=f"{KIND_MAILBOX_SYNC}:{org_id}:{int(run_at.timestamp())}",
            payload={"org_id": org_id},
            run_at=run_at,
        )
    return {"mailboxes": queued}


def _handle_mailbox_sync_one(db: Session, job: Job, now) -> dict[str, Any]:
    """One sync pass over one mailbox, whatever provider it belongs to."""
    from .integrations.providers import ProviderError
    from .integrations.sync import sync_mailbox

    connection = db.get(MailboxConnection, job.payload.get("mailbox_id", ""))
    if connection is None or connection.status != "active":
        return {"skipped": "mailbox gone or inactive"}
    try:
        summary = sync_mailbox(db, connection, now=now)
    except ProviderError as exc:
        # One broken mailbox writes its error to its own row and does not fail
        # the job: a revoked token would otherwise retry-and-park forever while
        # the healthy mailboxes carry on regardless.
        logger.warning("mailbox sync failed for %s: %s", connection.address, exc)
        connection.error = str(exc)
        db.flush()
        return {"error": str(exc), "address": connection.address}
    return {
        "address": connection.address,
        "provider": connection.provider,
        "seen": summary.seen,
        "matched": summary.matched,
        "initialized": summary.initialized,
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
    """One batch of the historical import; re-enqueues itself until done.

    Every key carries the run stamp minted when the button was pressed. It used
    to be ``mailbox.backfill:<id>:<offset>``, fixed for all time, and ``enqueue``
    returns the existing row for a key forever -- including one that already
    succeeded. So the first import worked and every later one silently returned
    the old finished job while the endpoint answered "queued". The offsets were
    poisoned too, so even a fresh walk collided with the previous run's keys.
    """
    from .integrations.backfill import backfill_mailbox_history
    from .models import MailboxConnection

    connection = db.get(MailboxConnection, job.payload.get("mailbox_id", ""))
    if connection is None or connection.status != "active":
        return {"skipped": "mailbox gone or inactive"}

    run = str(job.payload.get("run") or "")
    offset = int(job.payload.get("offset") or 0)
    state = dict(connection.backfill_state or {})
    # A stale batch from a run the user has since restarted must not write over
    # the new run's counters.
    if run and state.get("run") and state["run"] != run:
        return {"skipped": "superseded by a newer import"}

    result = backfill_mailbox_history(db, connection, offset=offset, now=now)
    next_offset = result.get("next_offset")

    state["status"] = "running" if next_offset is not None else "done"
    state["run"] = run or state.get("run", "")
    state["total"] = int(result.get("total") or state.get("total") or 0)
    state["done"] = min(int(next_offset or state["total"]), state["total"])
    state["imported"] = int(state.get("imported") or 0) + int(result.get("ingested") or 0)
    state["note"] = result.get("note") or ""
    if next_offset is None:
        state["finished_at"] = now.isoformat()
    # Reassigned, not mutated: SQLAlchemy does not notice an in-place edit of a
    # JSON column, and a silently unsaved counter is exactly the blindness this
    # is meant to cure.
    connection.backfill_state = state

    if next_offset is not None:
        enqueue(
            db,
            org_id=job.org_id,
            kind=KIND_MAILBOX_BACKFILL,
            idempotency_key=(
                f"{KIND_MAILBOX_BACKFILL}:{connection.id}:{run}:{next_offset}"
            ),
            payload={
                "org_id": job.org_id,
                "mailbox_id": connection.id,
                "offset": next_offset,
                "run": run,
            },
            run_at=now + timedelta(seconds=30),
        )
    return result


def _handle_jobs_parse(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.parse import handle_parse

    return handle_parse(db, job, now)


def _handle_jobs_match(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.engine import dispatch_match_tick

    org_id = job.payload.get("org_id") or job.org_id
    result = dispatch_match_tick(db, org_id, now=now)
    _schedule_next_tick(db, org_id=org_id, kind=KIND_JOBS_MATCH, now=now)
    return result


def _handle_jobs_match_one(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.engine import handle_match_one

    return handle_match_one(db, job, now)


def _handle_jobs_scrape(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.scrape import dispatch_scrape_tick

    org_id = job.payload.get("org_id") or job.org_id
    result = dispatch_scrape_tick(db, org_id, now=now)
    _schedule_next_tick(db, org_id=org_id, kind=KIND_JOBS_SCRAPE, now=now)
    return result


def _handle_jobs_scrape_one(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.scrape import handle_scrape_one

    return handle_scrape_one(db, job, now)


def _handle_crm_discovery(db: Session, job: Job, now) -> dict[str, Any]:
    """Fan a sweep out to one job per mailbox.

    Deliberately not serialized with ``jobs.has_open_job``: that is org-wide
    with no mailbox dimension, so it would let the first mailbox start and
    silently block every other one.

    Keys carry the dispatch timestamp because ``enqueue`` returns the existing
    row for a key forever, including a succeeded one. A fixed key would make
    the second sweep a no-op that reports success.
    """
    from .integrations.discovery import reset_for_rerun, start_state, window_start

    org_id = job.payload.get("org_id") or job.org_id
    only = job.payload.get("mailbox_id") or ""
    stamp = int(now.timestamp())
    since = window_start(now)

    query = select(MailboxConnection).where(
        MailboxConnection.org_id == org_id,
        MailboxConnection.status == "active",
        # The same switch that governs thread sync. Discovery stores no
        # content, but it does read a year of metadata, and inventing a second
        # consent surface for that would be worse than reusing the one that
        # already means "yes, read my mailbox".
        MailboxConnection.thread_sync_enabled.is_(True),
    )
    if only:
        query = query.where(MailboxConnection.id == only)
    mailboxes = db.scalars(query.order_by(MailboxConnection.id)).all()
    if not mailboxes:
        return {"mailboxes": 0, "reason": "no mailbox with thread sync enabled"}

    # Counters accumulate, so a re-run over the same window would double them.
    reset_for_rerun(db, org_id=org_id)

    for index, connection in enumerate(mailboxes):
        connection.discovery_state = start_state(since=since, now=now)
        run_at = now + timedelta(seconds=10 * index)
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_CRM_DISCOVERY_ONE,
            idempotency_key=f"{KIND_CRM_DISCOVERY_ONE}:{connection.id}:{stamp}:0",
            payload={
                "org_id": org_id,
                "mailbox_id": connection.id,
                "sweep": stamp,
                "page": 0,
                "since": since.isoformat(),
            },
            run_at=run_at,
        )
    return {"mailboxes": len(mailboxes), "sweep": stamp}


def _handle_crm_discovery_one(db: Session, job: Job, now) -> dict[str, Any]:
    """A bounded number of Sent-Items pages for one mailbox, then re-enqueue."""
    from datetime import datetime as _datetime

    from .integrations.discovery import (
        advance_state,
        build_context,
        fail_state,
        sweep_page,
    )
    from .integrations.providers import ProviderError

    settings = get_settings()
    connection = db.get(MailboxConnection, job.payload.get("mailbox_id", ""))
    if connection is None or connection.status != "active":
        return {"skipped": "mailbox gone or inactive"}
    if not connection.thread_sync_enabled:
        return {"skipped": "thread sync disabled"}

    raw_since = job.payload.get("since")
    # Frozen at dispatch: a sweep that takes hours must not have a lower bound
    # that creeps forward underneath it.
    since = _datetime.fromisoformat(raw_since) if raw_since else None
    if since is None:
        from .integrations.discovery import window_start

        since = window_start(now)

    page = int(job.payload.get("page") or 0)
    cursor = (connection.discovery_state or {}).get("cursor") or ""
    # One whole-table read per job, not per page.
    context = build_context(db, org_id=connection.org_id, now=now)

    scanned = found = 0
    pages_done = 0
    for _ in range(max(settings.discovery_pages_per_job, 1)):
        try:
            result = sweep_page(
                db, connection, context=context, since=since, cursor=cursor
            )
        except ProviderError as exc:
            # A revoked token must not retry until the job parks. Record it,
            # leave the cursor alone, and stop.
            logger.warning("discovery failed for %s: %s", connection.address, exc)
            connection.error = str(exc)
            fail_state(connection, str(exc))
            return {"error": str(exc)}

        # Counters and cursor advance together, after the page is recorded, so
        # a crash replays that page rather than skipping it.
        advance_state(connection, result)
        scanned += result["scanned"]
        found += result["found"]
        pages_done += 1
        cursor = result["next_cursor"]
        if not cursor:
            break

    if cursor and page + pages_done < settings.discovery_max_pages:
        enqueue(
            db,
            org_id=connection.org_id,
            kind=KIND_CRM_DISCOVERY_ONE,
            idempotency_key=(
                f"{KIND_CRM_DISCOVERY_ONE}:{connection.id}:"
                f"{job.payload.get('sweep')}:{page + pages_done}"
            ),
            payload={**job.payload, "page": page + pages_done},
            run_at=now + timedelta(seconds=30),
        )
    elif cursor:
        logger.warning(
            "discovery hit the page cap for %s after %s pages",
            connection.address,
            page + pages_done,
        )
        advance_state(connection, {"scanned": 0, "found": 0, "next_cursor": ""})

    return {"scanned": scanned, "found": found, "pages": pages_done}


def _handle_crm_discovery_import(db: Session, job: Job, now) -> dict[str, Any]:
    """Pull real history for people who were just promoted.

    Not ``mailbox.backfill``: that job's key is fixed per offset, so after one
    import has ever run, enqueuing it again returns the succeeded row and does
    nothing at all. It also walks the whole org address index, which would mean
    one provider search per address to import two people.
    """
    from .integrations.backfill import backfill_mailbox_history
    from .integrations.providers import ProviderError

    addresses = [a for a in (job.payload.get("addresses") or []) if a]
    if not addresses:
        return {"ingested": 0, "reason": "nothing to import"}

    org_id = job.payload.get("org_id") or job.org_id
    mailboxes = db.scalars(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.status == "active",
            MailboxConnection.thread_sync_enabled.is_(True),
        )
    ).all()

    ingested = 0
    for connection in mailboxes:
        try:
            result = backfill_mailbox_history(
                db, connection, addresses=addresses, now=now
            )
        except ProviderError as exc:
            logger.warning("promote import failed for %s: %s", connection.address, exc)
            connection.error = str(exc)
            continue
        ingested += int(result.get("ingested") or 0)
    return {"ingested": ingested, "addresses": len(addresses)}


def _handle_intake_turn(db: Session, job: Job, now) -> dict[str, Any]:
    """Advance one text intake thread.

    The same ``run_turn`` the chat route calls inline. Having one function with
    two callers is the point: a bug fixed for the widget is fixed for SMS, and
    the two channels cannot drift into asking different questions.
    """
    from .messaging.conversation import run_turn
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    result = run_turn(db, thread=thread)
    return {
        "thread_id": thread.id,
        "action": result.action,
        "asked_field": result.asked_field,
        "sent": len(result.sent),
    }


def _handle_intake_extract(db: Session, job: Job, now) -> dict[str, Any]:
    from .intake.service import extract_call
    from .models import IntakeCall

    call = db.get(IntakeCall, job.payload.get("intake_call_id"))
    if call is None:
        return {"skipped": "missing_call"}
    # Scoped even though the id came from our own payload: a job row and a call
    # row are both org-owned, and a mismatch means something is very wrong.
    if call.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    result = extract_call(db, call)
    if result is None:
        return {"skipped": f"call_{call.status}"}
    return {
        "intake_call_id": call.id,
        "candidate_id": call.candidate_id,
        "extractor": result.extractor,
        "required_complete": result.coverage.required_complete,
        "review_needed": call.review_needed,
    }


HANDLERS = {
    KIND_CRM_DISCOVERY: _handle_crm_discovery,
    KIND_CRM_DISCOVERY_ONE: _handle_crm_discovery_one,
    KIND_CRM_DISCOVERY_IMPORT: _handle_crm_discovery_import,
    KIND_OUTREACH_TICK: _handle_outreach_tick,
    KIND_OUTREACH_SEND: _handle_outreach_send,
    KIND_AGENT_TICK: _handle_agent_tick,
    KIND_MAILBOX_SYNC: _handle_mailbox_sync,
    KIND_MAILBOX_SYNC_ONE: _handle_mailbox_sync_one,
    KIND_MAILBOX_BACKFILL: _handle_mailbox_backfill,
    KIND_REPLY_DRAFT: _handle_reply_draft,
    KIND_JOBS_PARSE: _handle_jobs_parse,
    KIND_JOBS_MATCH: _handle_jobs_match,
    KIND_JOBS_MATCH_ONE: _handle_jobs_match_one,
    KIND_JOBS_SCRAPE: _handle_jobs_scrape,
    KIND_JOBS_SCRAPE_ONE: _handle_jobs_scrape_one,
    KIND_INTAKE_TURN: _handle_intake_turn,
    KIND_INTAKE_EXTRACT: _handle_intake_extract,
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

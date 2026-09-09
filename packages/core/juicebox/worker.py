"""Background worker: claim jobs, run them, schedule the next tick.

    juicebox work          # loop
    juicebox work --once   # drain the queue and exit (what `make worker` runs)
"""

from __future__ import annotations

import logging
import os
import socket
import time
from datetime import UTC, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .jobs import (
    INTERACTIVE_KINDS,
    KIND_AGENT_TICK,
    KIND_APP_PACKAGE,
    KIND_APP_SUBMIT,
    KIND_APP_SUBMIT_VERIFY,
    KIND_APPLY_LINK,
    KIND_CONNECTOR_INDEX,
    KIND_CRM_DISCOVERY,
    KIND_CRM_DISCOVERY_IMPORT,
    KIND_CRM_DISCOVERY_ONE,
    KIND_DOC_PARSE,
    KIND_INTAKE_ATTACHMENT,
    KIND_INTAKE_EXTRACT,
    KIND_INTAKE_MATCH_TEXT,
    KIND_INTAKE_REFERRAL,
    KIND_INTAKE_TURN,
    KIND_JOBS_DISCOVER_ONE,
    KIND_JOBS_FORMSPEC_ONE,
    KIND_JOBS_MATCH,
    KIND_JOBS_MATCH_ONE,
    KIND_JOBS_PARSE,
    KIND_JOBS_SCRAPE,
    KIND_JOBS_SCRAPE_ONE,
    KIND_LINKEDIN_SCRAPE,
    KIND_MAILBOX_BACKFILL,
    KIND_MAILBOX_SYNC,
    KIND_MAILBOX_SYNC_ONE,
    KIND_OUTREACH_FOLLOW_UP,
    KIND_OUTREACH_SEND,
    KIND_OUTREACH_TICK,
    KIND_PROFILE_INGEST,
    KIND_PROFILE_RESUME,
    KIND_REPLY_DRAFT,
    KIND_THREAD_NOTIFY,
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

    from .outreach.sequencer import HOLD_DEFER, process_enrollment, sending_hold

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
        result: dict[str, Any] = {"defer": True, "status": status}
        if sending_hold(db, enrollment.sequence, now=now):
            result["defer_until"] = now + HOLD_DEFER
        return result
    if status == "failed" and enrollment.status == "active":
        # The step is still due. Marking the job succeeded spends the
        # permanent idempotency key, and the tick cannot re-queue it — the
        # same trap `deferred` exists to avoid. Raise so `mark_failed`
        # retries; a stopped enrollment (suppressed, missing email) is
        # already finished and the job can succeed.
        error = (_message.error if _message is not None else "") or "send failed"
        raise RuntimeError(f"outreach send failed: {error}")
    return {"status": status}


def _handle_outreach_follow_up(db: Session, job: Job, now) -> dict[str, Any]:
    """Remind the sending mailbox that a candidate asked to hear back today."""
    from datetime import date

    from sqlalchemy.orm import selectinload

    from .models import Message
    from .outreach.follow_ups import send_follow_up_reminder

    enrollment = db.scalar(
        select(Enrollment)
        .where(Enrollment.id == job.payload.get("enrollment_id", ""))
        .options(selectinload(Enrollment.sequence).selectinload(Sequence.steps))
    )
    inbound = db.get(Message, job.payload.get("inbound_message_id", ""))
    if enrollment is None or inbound is None:
        return {"skipped": "enrollment or reply gone"}
    queued_for = None
    raw = job.payload.get("follow_up_after") or ""
    if raw:
        try:
            queued_for = date.fromisoformat(raw)
        except ValueError:
            queued_for = None
    result = send_follow_up_reminder(
        db, enrollment=enrollment, inbound=inbound, follow_up_after=queued_for, now=now
    )
    return {"result": result, "enrollment_id": enrollment.id}


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
    from .config import get_settings
    from .integrations.reply_drafting import create_reply_draft
    from .models import Message

    # The second half of the kill switch: a job queued before the feature was
    # switched off must not still write into somebody's mailbox.
    if not get_settings().reply_drafting_enabled:
        return {"skipped": "reply_drafting_disabled"}

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


def _handle_jobs_discover_one(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.discover_candidate import handle_discover_one

    return handle_discover_one(db, job, now)


def _handle_agent_tick(db: Session, job: Job, now) -> dict[str, Any]:
    from .agents.sourcing import run_due_agents

    org_id = job.payload.get("org_id") or job.org_id
    runs = run_due_agents(db, org_id=org_id, now=now)
    # Attempts waiting on mail that never came. Cheap, and the only thing
    # that turns a permanently "in flight" application into an honest one.
    from .applications.submission import expire_parked

    parked_expired = expire_parked(db, org_id=org_id, now=now)

    _schedule_next_tick(db, org_id=org_id, kind=KIND_AGENT_TICK, now=now)
    return {
        "agents": len(runs),
        "added": sum(run.added for run in runs),
        "enrolled": sum(run.enrolled for run in runs),
        "parked_expired": parked_expired,
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

    result = handle_match_one(db, job, now)

    # matches.ready: when a candidate rescore lands and they have a standing
    # thread, tell them -- at most once per day per candidate (the date bucket
    # is the idempotency), and only if cards actually clear the bar (the
    # notify handler stays silent otherwise).
    candidate_id = (job.payload or {}).get("candidate_id")
    if candidate_id and not result.get("skipped"):
        from .messaging.notify import EVENT_MATCHES_READY, notify_candidate

        notify_candidate(
            db,
            org_id=job.org_id,
            candidate_id=candidate_id,
            event=EVENT_MATCHES_READY,
            subject_id=f"{candidate_id}:{now.date().isoformat()}",
        )
    return result


def _handle_jobs_scrape(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.scrape import dispatch_scrape_tick

    org_id = job.payload.get("org_id") or job.org_id
    result = dispatch_scrape_tick(db, org_id, now=now)
    _schedule_next_tick(db, org_id=org_id, kind=KIND_JOBS_SCRAPE, now=now)
    return result


def _handle_jobs_scrape_one(db: Session, job: Job, now) -> dict[str, Any]:
    from .jobmatch.scrape import handle_scrape_one

    return handle_scrape_one(db, job, now)


def _handle_jobs_formspec_one(db: Session, job: Job, now) -> dict[str, Any]:
    from .applications.formspec import handle_formspec_one

    return handle_formspec_one(db, job, now)


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
        state = start_state(since=since, now=now)
        # The sweep stamp rides on the connection as well as on the payload so
        # a page job left over from an earlier sweep can tell it has been
        # superseded before it advances this sweep's cursor.
        state["sweep"] = stamp
        connection.discovery_state = state
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

    state = connection.discovery_state or {}
    sweep = str(job.payload.get("sweep") or "")
    # The same guard the backfill handler carries, for the same reason. Both
    # sweeps share one cursor -- it lives on the connection -- so a page from a
    # run the dispatcher has since restarted would walk the new run's cursor
    # forward past pages neither of them has read.
    if sweep and state.get("sweep") and str(state["sweep"]) != sweep:
        return {"skipped": "superseded by a newer sweep"}

    raw_since = job.payload.get("since")
    # Frozen at dispatch: a sweep that takes hours must not have a lower bound
    # that creeps forward underneath it.
    since = _datetime.fromisoformat(raw_since) if raw_since else None
    if since is None:
        from .integrations.discovery import window_start

        since = window_start(now)

    page = int(job.payload.get("page") or 0)
    cursor = state.get("cursor") or ""
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
    from .messaging.conversation import ACTION_DEFERRED, run_turn
    from .messaging.linq import maybe_mark_read, maybe_start_typing
    from .messaging.suppression import is_blocked
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    # Read, then typing, then the reply: the order a person produces, and
    # without the first the candidate watches "Delivered" while we think.
    #
    # Both reach the carrier, so both are contact. Someone who texted STOP and
    # later taps an old card would otherwise get a read receipt and a
    # "typing..." bubble from us -- the rails below stop the reply, but these
    # two run before any of them. `react_to_message` already checks this.
    if not is_blocked(db, thread=thread):
        maybe_mark_read(thread)
        maybe_start_typing(thread)

    # A tapback carries no text, so there is no message for `run_turn` to read
    # and no ordinal to resolve. The proposal is named on the job instead.
    proposal_id = job.payload.get("apply_proposal_id") or ""
    if proposal_id:
        from .messaging.manager import apply_to_proposal

        return apply_to_proposal(db, thread=thread, proposal_id=proposal_id)

    result = run_turn(db, thread=thread)
    if result.action == ACTION_DEFERRED and result.resume_at is not None:
        # Quiet hours. The turn re-runs when the recipient's send window
        # opens -- deferral is Job.run_at, not a scheduler.
        return {
            "defer": True,
            "defer_until": result.resume_at,
            "thread_id": thread.id,
            "action": result.action,
        }
    return {
        "thread_id": thread.id,
        "action": result.action,
        "asked_field": result.asked_field,
        "sent": len(result.sent),
    }


def _handle_doc_parse(db: Session, job: Job, now) -> dict[str, Any]:
    """Extract text from one uploaded document and read it into resume.v1.

    Parse problems land on the row (``error``), never as a handler raise: a
    scanned PDF is a data point about the upload, not a worker failure, and
    retrying it would extract the same nothing.
    """
    from . import storage
    from .docgen.parse import extract_text, parse_resume_text
    from .docgen.resume import RESUME_SCHEMA_VERSION
    from .models import CandidateDocument

    document_id = job.payload.get("document_id") or ""
    doc = db.get(CandidateDocument, document_id)
    if doc is None:
        return {"skipped": "document not found", "document_id": document_id}
    if doc.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if doc.kind != "resume_uploaded":
        return {"skipped": f"kind {doc.kind} is not parseable"}

    if not doc.text_extracted:
        data = storage.read_bytes(doc.storage_path)
        doc.text_extracted = extract_text(data)

    structured, error = parse_resume_text(doc.text_extracted)
    doc.structured = structured
    doc.error = error
    if structured:
        doc.schema_version = RESUME_SCHEMA_VERSION
    # The file itself is fine either way -- ready with an error note beats a
    # "failed" upload the candidate would be asked to redo for our benefit.
    doc.status = "ready"
    db.flush()

    promoted = {}
    if structured and doc.candidate_id:
        # Promotion is additive by construction (enrich.promote), so applying
        # it automatically after a parse is safe: nothing present is rewritten,
        # summary and skills move only into blanks, and the resume.v1 stays on
        # this row for review and future re-promotion.
        from .enrich.promote import payload_from_resume_v1, promote
        from .jobmatch.engine import enqueue_match_for_candidate
        from .models import Candidate

        candidate = db.get(Candidate, doc.candidate_id)
        if candidate is not None:
            parsed = payload_from_resume_v1(
                structured, fallback_name=candidate.full_name or ""
            )
            result = promote(db, org_id=job.org_id, parsed=parsed, candidate=candidate)
            promoted = {
                "added_experiences": result.added_experiences,
                "added_educations": result.added_educations,
                "filled_fields": result.filled_fields,
            }
            enqueue_match_for_candidate(db, candidate, now=now)
            # Their own upload is the best source the ATS copy will ever have.
            _enqueue_profile_resume(
                db, org_id=job.org_id, candidate_id=candidate.id, event=f"doc:{doc.id}"
            )
            promoted["thread"] = _resume_settles_thread(db, doc=doc, candidate=candidate)
    elif not structured:
        promoted["thread"] = _resume_fails_thread(db, doc=doc, error=error)

    return {
        "document_id": doc.id,
        "parsed": bool(structured),
        "error": error,
        "text_chars": len(doc.text_extracted or ""),
        **promoted,
    }


def _resume_settles_thread(db: Session, *, doc, candidate) -> dict[str, Any]:
    """A parsed resume answers the profile question, so the thread moves on.

    The upload page and its parse have always existed; nothing connected them
    back to the conversation that asked. Somebody who took the resume option
    uploaded a file, got a "got it" web page, and then sat in a thread still
    waiting for a LinkedIn URL.

    Written into the same ``_linkedin.scrape`` slot the scrape uses, because
    the confirm step reads that slot and the two routes answer the same
    questions. The record fields go through ``apply_profile_to_record``, the
    same merge -- so a title the candidate already corrected still wins.
    """
    from .messaging.linkedin_flow import (
        apply_profile_to_record,
        profile_from_resume,
        update_flow_meta,
    )
    from .models import IntakeThread

    thread = db.get(IntakeThread, doc.thread_id) if doc.thread_id else None
    if thread is None or thread.org_id != doc.org_id or thread.status == "stopped":
        return {"skipped": "no thread"}

    # The upload is the candidate's turn, and the thread has to see it as one.
    # Their last message was our own question, and `run_turn` refuses to send
    # two outbound in a row -- so without this the conversation holds forever
    # waiting for a reply that already arrived, as a file, on a web page.
    # Deterministic native id: a redelivered parse must not re-record it.
    from .messaging.threads import record_inbound

    record_inbound(
        db,
        thread=thread,
        body=f"[sent a resume: {doc.filename or 'resume.pdf'}]",
        provider_native_id=f"resume:{doc.id}",
    )

    profile = profile_from_resume(candidate, doc.structured or {})
    thread.record = apply_profile_to_record(thread.record, profile)
    if not (thread.contact_name or "").strip() and profile.full_name:
        thread.contact_name = profile.full_name[:240]
    if profile.url and not thread.contact_linkedin:
        thread.contact_linkedin = profile.url[:400]
    update_flow_meta(thread, scrape=profile.to_dict(), error="", source="resume")
    db.flush()

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{thread.id}:after-resume:{doc.id}",
        payload={"thread_id": thread.id},
    )
    return {"thread_id": thread.id, "advanced": True}


def _resume_fails_thread(db: Session, *, doc, error: str) -> dict[str, Any]:
    """A resume that would not parse still answers the thread — with a no.

    Without this the conversation holds forever: the settle path only runs on
    a successful parse, so a scanned PDF (or a parse with no model configured)
    left the person waiting on a question they had already answered with a
    file. Writing the error into the flow meta is exactly what a failed scrape
    does, and lands in the same place: the gap-fill questions, asked honestly.
    """
    from .messaging.linkedin_flow import update_flow_meta
    from .messaging.threads import record_inbound
    from .models import IntakeThread

    thread = db.get(IntakeThread, doc.thread_id) if doc.thread_id else None
    if thread is None or thread.org_id != doc.org_id or thread.status == "stopped":
        return {"skipped": "no thread"}

    record_inbound(
        db,
        thread=thread,
        body=f"[sent a resume we could not read: {doc.filename or 'resume.pdf'}]",
        provider_native_id=f"resume:{doc.id}",
    )
    update_flow_meta(thread, error=error or "resume parse failed", source="resume")
    db.flush()

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{thread.id}:after-resume:{doc.id}",
        payload={"thread_id": thread.id},
    )
    return {"thread_id": thread.id, "advanced": False, "error": error}


def _handle_intake_attachment(db: Session, job: Job, now) -> dict[str, Any]:
    """Download one inbound message's attachments; keep the resume if one is.

    All the judgment lives in ``messaging.attachments`` — this handler only
    loads the thread and hands over, like every other thin handler here.
    """
    from .messaging.attachments import ingest_inbound_attachments
    from .messaging.linq import InboundAttachment
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if thread.status == "stopped":
        return {"skipped": "thread stopped"}

    attachments = [
        InboundAttachment.from_dict(item)
        for item in job.payload.get("attachments") or []
        if isinstance(item, dict)
    ]
    if not attachments:
        return {"skipped": "no attachments"}

    return ingest_inbound_attachments(
        db,
        thread=thread,
        message_id=str(job.payload.get("message_id") or ""),
        attachments=attachments,
        had_text=bool(job.payload.get("had_text")),
    )


def _handle_intake_referral(db: Session, job: Job, now) -> dict[str, Any]:
    """Answer one iMessage group chat, record who was referred, then leave.

    All the judgment lives in ``messaging.referrals`` — this handler only loads
    the thread and hands over. No turn is ever run on a group thread: the
    intake agent asks one person about their own preferences, and there is more
    than one person in the room.
    """
    from .messaging.referrals import handle_group
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if thread.status == "stopped":
        return {"skipped": "thread stopped"}

    return handle_group(db, thread=thread)


def _enqueue_profile_resume(db: Session, *, org_id: str, candidate_id: str, event: str) -> None:
    """Ask for a refresh of the candidate's profile-level ATS resume.

    The event suffix keeps regeneration honest: each promotion that lands new
    facts gets its own idempotency key, while a redelivered job reuses the one
    it already made.
    """
    enqueue(
        db,
        org_id=org_id,
        kind=KIND_PROFILE_RESUME,
        idempotency_key=f"{KIND_PROFILE_RESUME}:{candidate_id}:{event}",
        payload={"candidate_id": candidate_id},
    )


def _handle_profile_resume(db: Session, job: Job, now) -> dict[str, Any]:
    """Refresh one candidate's profile-level ATS resume (docgen.profile)."""
    from .docgen.profile import refresh_profile_resume
    from .models import Candidate

    candidate_id = job.payload.get("candidate_id") or ""
    candidate = db.get(Candidate, candidate_id)
    if candidate is None:
        return {"skipped": "candidate not found", "candidate_id": candidate_id}
    if candidate.org_id != job.org_id:
        return {"skipped": "org_mismatch"}

    doc, error = refresh_profile_resume(db, candidate)
    if doc is None:
        return {"skipped": error, "candidate_id": candidate_id}
    return {
        "document_id": doc.id,
        "status": doc.status,
        "error": error,
        "candidate_id": candidate_id,
    }


def _handle_profile_ingest(db: Session, job: Job, now) -> dict[str, Any]:
    """Fetch one LinkedIn profile and promote it onto the CRM.

    The cache discipline is the whole design: a fresh check is skipped, a real
    miss is stamped, a provider failure re-raises so the queue's backoff
    retries it -- and never stamps, so a vendor's quota wall cannot mark a
    person permanently unfetchable.
    """
    from datetime import timedelta

    from sqlalchemy import select as _select

    from .config import get_settings as _get_settings
    from .enrich.linkedin_profile import (
        ProfileNotFound,
        get_profile_provider,
        linkedin_slug,
        map_profile,
    )
    from .enrich.promote import promote
    from .jobmatch.engine import enqueue_match_for_candidate
    from .models import Candidate, CandidateDocument, IntakeThread

    linkedin_url = str(job.payload.get("linkedin_url") or "")
    slug = linkedin_slug(linkedin_url)
    if not slug:
        return {"skipped": "not a linkedin profile url", "url": linkedin_url}

    candidate = None
    candidate_id = job.payload.get("candidate_id") or ""
    if candidate_id:
        candidate = db.get(Candidate, candidate_id)
        if candidate is not None and candidate.org_id != job.org_id:
            return {"skipped": "org_mismatch"}
    if candidate is None:
        candidate = db.scalar(
            _select(Candidate).where(
                Candidate.org_id == job.org_id,
                Candidate.source == "linkedin_ingest",
                Candidate.source_id == slug,
            )
        )

    settings = _get_settings()
    if candidate is not None and candidate.profile_ingest_checked_at is not None:
        checked = candidate.profile_ingest_checked_at
        if checked.tzinfo is None:

            checked = checked.replace(tzinfo=UTC)
        if now - checked < timedelta(days=settings.profile_ingest_recheck_days):
            return {"skipped": "recently_checked", "candidate_id": candidate.id}

    provider = get_profile_provider()
    try:
        raw = provider.fetch(linkedin_url)
    except ProfileNotFound:
        if candidate is not None:
            candidate.profile_ingest_checked_at = now
            candidate.profile_ingest_error = "profile not found or not visible"
            db.flush()
        return {"not_found": True, "url": linkedin_url}

    parsed = map_profile(raw, linkedin_url=linkedin_url)
    result = promote(db, org_id=job.org_id, parsed=parsed, candidate=candidate)
    promoted = db.get(Candidate, result.candidate_id)
    if promoted is None:
        return {"skipped": "promotion produced no candidate", "url": linkedin_url}
    promoted.profile_ingest_checked_at = now
    promoted.profile_ingest_error = ""

    # The parse artifact, for review and for re-promotion under future rules.
    db.add(
        CandidateDocument(
            org_id=job.org_id,
            candidate_id=promoted.id,
            kind="other",
            filename=f"linkedin-{slug}.json",
            content_type="application/json",
            status="ready",
            structured=parsed,
            source="linkedin_ingest",
            thread_id=job.payload.get("thread_id") or None,
        )
    )

    thread_id = job.payload.get("thread_id") or ""
    if thread_id:
        thread = db.get(IntakeThread, thread_id)
        if thread is not None and thread.org_id == job.org_id and not thread.candidate_id:
            thread.candidate_id = promoted.id

    enqueue_match_for_candidate(db, promoted, now=now)
    _enqueue_profile_resume(
        db, org_id=job.org_id, candidate_id=promoted.id, event=f"ingest:{job.id}"
    )

    if thread_id:
        from .messaging.notify import EVENT_PROFILE_READY, notify_candidate

        notify_candidate(
            db,
            org_id=job.org_id,
            candidate_id=promoted.id,
            event=EVENT_PROFILE_READY,
            subject_id=slug,
        )
    db.flush()
    return {
        "candidate_id": promoted.id,
        "created": result.created,
        "added_experiences": result.added_experiences,
        "added_educations": result.added_educations,
    }


def _handle_app_package(db: Session, job: Job, now) -> dict[str, Any]:
    """Build one application package. Failures re-raise so backoff retries."""
    from .applications.service import build_package
    from .messaging.notify import EVENT_PACKAGE_READY, notify_candidate
    from .models import Application

    application_id = job.payload.get("application_id") or ""
    application = db.get(Application, application_id)
    if application is None:
        return {"skipped": "application not found", "application_id": application_id}
    if application.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if application.status not in {"draft", "prepared"}:
        return {"skipped": f"status {application.status} does not take a package"}
    result = build_package(db, application)

    # The package exists and the yes is already on the row (the thread's "apply
    # to 2" records it at creation), so this is where an automated attempt
    # becomes possible. Most postings have no adapter and the answer is the
    # hand-off we already do -- which is why `auto_submit` reports rather than
    # raises.
    from .applications.submission import auto_submit

    submit_job, reason = auto_submit(db, application)
    result["submission"] = reason
    # Thumbs-up → package → submit. A queued attempt announces itself when it
    # lands ("I submitted it"); a "ready to review" text first would be a
    # second yes for a document that did not exist when they tapped the card.
    #
    # Missing work authorization is the one question chat still asks, and it
    # is asked here rather than behind a review message. A genuine hand-off
    # (no adapter, login wall) still sends the package link.
    waiting = reason.startswith("waiting on required form answers")
    if submit_job is None and not waiting:
        notify_candidate(
            db,
            org_id=job.org_id,
            candidate_id=application.candidate_id,
            event=EVENT_PACKAGE_READY,
            subject_id=application.id,
        )
    else:
        from .applications.followups import ask_for_missing

        result["followup"] = ask_for_missing(db, application)
    return result


def _handle_apply_link(db: Session, job: Job, now) -> dict[str, Any]:
    from .messaging.apply_link import handle_apply_link

    return handle_apply_link(db, job, now)


def _handle_thread_notify(db: Session, job: Job, now) -> dict[str, Any]:
    from .messaging.notify import handle_notify

    return handle_notify(db, job, now)


def _handle_intake_match_text(db: Session, job: Job, now) -> dict[str, Any]:
    from .messaging.voice_handoff import handle_match_text

    return handle_match_text(db, job, now)


def _paid_profile_fallback(db: Session, *, thread, url: str):
    """The scrape missed; buy the profile instead. Returns (profile, error)."""
    from .enrich.profile_sources import paid_lookup
    from .models import Candidate

    candidate = db.get(Candidate, thread.candidate_id) if thread.candidate_id else None
    profile, error = paid_lookup(
        db,
        org_id=thread.org_id,
        linkedin_url=url,
        candidate=candidate,
        thread_id=thread.id,
    )
    if profile is not None and not thread.candidate_id:
        # promote() created or matched a candidate row; bind the thread to it
        # so the rest of the conversation, and every notification after it,
        # has somebody to be about.
        from sqlalchemy import select as _select

        from .enrich.linkedin_profile import linkedin_slug

        slug = linkedin_slug(url)
        found = db.scalar(
            _select(Candidate).where(
                Candidate.org_id == thread.org_id,
                Candidate.source == "linkedin_ingest",
                Candidate.source_id == slug,
            )
        )
        if found is not None:
            thread.candidate_id = found.id
    return profile, error


def _handle_linkedin_scrape(db: Session, job: Job, now) -> dict[str, Any]:
    """Fetch a LinkedIn profile and re-enqueue the thread's next turn.

    Two sources. ``profile_sources.scrape_first`` decides whether the free
    browser scrape is tried before the paid provider, and by default it is not
    -- a scrape with no session costs a minute to fail and the candidate spends
    that minute watching a typing indicator. See ``enrich.profile_sources``.
    """
    from .enrich.profile_sources import scrape_first
    from .messaging.linkedin_flow import (
        FLOW_META_KEY,
        apply_profile_to_record,
        update_flow_meta,
    )
    from .messaging.linq import maybe_start_typing
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    url = job.payload.get("linkedin_url") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}

    maybe_start_typing(thread)
    profile = None
    failure = ""
    if scrape_first():
        from .enrich.linkedin_scrape import ScrapeError, scrape_profile

        try:
            profile = scrape_profile(url)
        except ScrapeError as exc:
            logger.warning("linkedin scrape failed for thread %s: %s", thread.id, exc)
            failure = str(exc)
        except Exception as exc:
            logger.exception("linkedin scrape crashed for thread %s", thread.id)
            failure = str(exc)

    if profile is None:
        # The paid provider: either the scrape missed, or it was skipped
        # because it could not have hit. A scrape hitting an auth wall says
        # nothing about whether this person exists, so spend a lookup rather
        # than drop the candidate into a question batch they did not need.
        #
        # Guarded exactly like the scrape above, and for a reason found the
        # hard way: this parses a third party's payload, and an unreadable
        # date in it used to raise straight through the handler. The job died
        # before the turn below was enqueued, so the thread went silent -- a
        # candidate mid-conversation got no reply and no error, and the retry
        # failed the same way five times.
        paid_error = ""
        try:
            profile, paid_error = _paid_profile_fallback(db, thread=thread, url=url)
        except Exception as exc:  # a live thread must still answer
            logger.exception("paid profile lookup crashed for thread %s", thread.id)
            profile, paid_error = None, str(exc)
        if profile is None:
            update_flow_meta(thread, error=(failure or paid_error)[:400], scrape=None)
        elif thread.candidate_id:
            # The paid lookup promoted a full history; the candidate's ATS
            # resume should exist the moment their profile does.
            _enqueue_profile_resume(
                db, org_id=thread.org_id, candidate_id=thread.candidate_id,
                event=f"scrape:{job.id}",
            )

    if profile is not None:
        thread.record = apply_profile_to_record(thread.record, profile)
        # Re-attach meta after merge_record, which copies unknown keys but
        # apply_profile_to_record goes through merge_record of empty+incoming
        # and preserves _linkedin from current.
        if not (thread.contact_name or "").strip() and profile.full_name:
            thread.contact_name = profile.full_name[:240]
        if profile.url and not thread.contact_linkedin:
            thread.contact_linkedin = profile.url[:400]
        update_flow_meta(thread, scrape=profile.to_dict(), error="")
        # merge_record copies _linkedin from current; update_flow_meta then
        # writes scrape onto it. If apply wiped the key, restore from profile.
        if FLOW_META_KEY not in (thread.record or {}):
            update_flow_meta(thread, scrape=profile.to_dict(), error="")

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{thread.id}:after-scrape",
        payload={"thread_id": thread.id},
    )
    return {"thread_id": thread.id, "scraped": bool((thread.record or {}).get("_linkedin", {}).get("scrape"))}


def _handle_connector_index(db: Session, job: Job, now) -> dict[str, Any]:
    """One targeted index of a connected app, then resume the conversation.

    Guarded like ``_handle_linkedin_scrape`` and for the same hard-won reason:
    this parses third-party payloads mid-conversation, and an exception that
    escapes before the thread is answered leaves a person waiting on a reply
    that will never come. Every branch below therefore ends in exactly one
    conversational consequence — a stored resume (whose parse settles the
    thread), or a synthetic inbound plus a turn.
    """
    from .connectors import extract as connector_extract
    from .connectors import get_connector
    from .messaging import identity
    from .messaging.attachments import store_thread_resume
    from .messaging.linkedin_flow import update_flow_meta
    from .messaging.linq import maybe_start_typing
    from .messaging.threads import record_inbound
    from .models import CandidateConnection, IntakeThread

    connection = db.get(CandidateConnection, job.payload.get("connection_id") or "")
    if connection is None:
        return {"skipped": "connection not found"}
    if connection.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    thread = db.get(IntakeThread, connection.thread_id)
    if thread is None or thread.org_id != connection.org_id:
        return {"skipped": "thread not found"}
    if connection.status != "active":
        return {"skipped": f"connection {connection.status}"}
    if thread.status == "stopped":
        # Indexing somebody who opted out between tapping connect and this
        # job running would be reading their mailbox after they said stop.
        connection.index_state = {**(connection.index_state or {}), "status": "failed",
                                  "error": "thread stopped"}
        db.flush()
        return {"skipped": "thread stopped"}

    run = str(job.payload.get("run") or "")
    connection.index_state = {
        **(connection.index_state or {}),
        "status": "running",
        "run": run,
        "started_at": now.isoformat(),
    }
    db.flush()
    maybe_start_typing(thread)

    from .messaging.manager import MODE_MANAGING

    label = connection.provider
    state: dict[str, Any] = dict(connection.index_state or {})
    stored_doc_id = ""
    failure = ""
    try:
        connector = get_connector(connection.provider)
        label = connector.label

        hits = connector.list_resume_hits(db, connection)
        state["resume_hits"] = len(hits)
        picked = connector_extract.pick_resume(db, connector, connection, hits)

        if picked is not None:
            data, hit = picked
            candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
            if candidate_id:
                doc = store_thread_resume(
                    db,
                    thread=thread,
                    candidate_id=candidate_id,
                    data=data,
                    filename=hit.filename,
                    source="connector",
                )
                stored_doc_id = doc.id
                state["resume_doc_id"] = doc.id
                connection.candidate_id = candidate_id
                # The connector chose a likely file, not a final answer.  Give
                # the candidate a direct route to inspect or replace it.
                from .messaging.channels import OutboundMessage
                from .messaging.conversation import deliver
                from .portal.links import mint_portal_link

                link = mint_portal_link(
                    db,
                    org_id=thread.org_id,
                    candidate_id=candidate_id,
                    channel="sms",
                    next_path="/documents",
                )
                deliver(
                    db,
                    thread=thread,
                    message=OutboundMessage(
                        body=f"I found a resume in your connected {label}. Review or replace it here: {link}",
                    ),
                )
            else:
                # A grant with no email is possible (GitHub-only), so the
                # bytes wait exactly the way an early iMessage attachment
                # does: stashed on the thread, attached by the turn that
                # captures an address.
                from . import storage
                from .docgen.parse import SUFFIX_BY_MIME, sniff_resume_type
                from .messaging.attachments import PENDING_KEY

                suffix = SUFFIX_BY_MIME.get(sniff_resume_type(data), ".pdf")
                relpath = storage.pending_attachment_relpath(
                    org_id=thread.org_id,
                    thread_id=thread.id,
                    message_id=f"connect-{run or connection.id}",
                    suffix=suffix,
                )
                storage.save_bytes(relpath, data)
                update_flow_meta(
                    thread,
                    **{PENDING_KEY: {"relpath": relpath, "filename": hit.filename,
                                     "content_type": hit.content_type}},
                )
                state["resume_pending"] = True

        if not stored_doc_id:
            profile = connector.fetch_profile(db, connection)
            to_linked = connector_extract.to_linkedin_profile(profile)
            # A managing thread is past its confirm step; the record still
            # gains what merge_record allows, but the confirm slot is not
            # rewritten under an established conversation.
            if thread.mode != MODE_MANAGING:
                from .messaging.linkedin_flow import apply_profile_to_record

                thread.record = apply_profile_to_record(thread.record, to_linked)
                if not (thread.contact_name or "").strip() and to_linked.full_name:
                    thread.contact_name = to_linked.full_name[:240]
                update_flow_meta(
                    thread,
                    scrape=connector_extract.scrape_payload(
                        profile, provider=connection.provider
                    ),
                    error="",
                    source=f"connector:{connection.provider}",
                )
            if profile.skills and thread.candidate_id:
                from .models import Candidate

                candidate = db.get(Candidate, thread.candidate_id)
                if candidate is not None:
                    connector_extract.apply_skills_to_candidate(candidate, profile.skills)

        if connection.provider == "google":
            # One count, no content — see connectors.google.job_search_signal.
            try:
                from .connectors.google import job_search_signal

                state["signals"] = job_search_signal(connector, db, connection)
            except Exception:  # noqa: BLE001 - a signal is never worth a failure
                logger.debug("job-search signal fetch failed for %s", connection.id)
    except Exception as exc:
        logger.exception("connector index failed for thread %s", thread.id)
        failure = str(exc)
        if thread.mode != MODE_MANAGING and not (
            (thread.record or {}).get("_linkedin", {}).get("scrape")
        ):
            update_flow_meta(
                thread, error=failure[:400], source=f"connector:{connection.provider}"
            )

    state["status"] = "failed" if failure else "done"
    state["finished_at"] = utcnow().isoformat()
    if failure:
        state["error"] = failure[:400]
    connection.index_state = state
    connection.last_indexed_at = utcnow()
    db.flush()

    # The stored-resume branch's consequence is its parse settling the thread
    # (_resume_settles_thread records the inbound and enqueues the turn); a
    # second trigger here would double-reply. Every other branch answers now.
    if not stored_doc_id:
        _, created = record_inbound(
            db,
            thread=thread,
            body=f"[connected {label}]",
            provider_native_id=f"connect:{connection.id}:{run or 'first'}",
        )
        if created:
            enqueue(
                db,
                org_id=thread.org_id,
                kind=KIND_INTAKE_TURN,
                idempotency_key=(
                    f"{KIND_INTAKE_TURN}:{thread.id}:after-connect:{connection.id}:"
                    f"{run or 'first'}"
                ),
                payload={"thread_id": thread.id},
            )

    return {
        "connection_id": connection.id,
        "provider": connection.provider,
        "resume_doc_id": stored_doc_id,
        "failed": bool(failure),
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


def _handle_app_submit(db: Session, job: Job, now) -> dict[str, Any]:
    """One submission attempt.

    Failures re-raise so the queue's backoff retries the *job*; the attempt row
    is already written either way, which is what keeps a retried job from
    looking like a submission that never happened.
    """
    from .applications.submission import submit_application
    from .models import Application

    application_id = job.payload.get("application_id") or ""
    application = db.get(Application, application_id)
    if application is None:
        return {"skipped": "application not found", "application_id": application_id}
    if application.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    # `submitting` is a legitimate state to arrive in: the previous run set it
    # and then the worker died, and `recover_stale_locks` requeued this job.
    # Skipping it there is what used to strand the application in `submitting`
    # with no path out but a hand transition. `submit_application` is idempotent
    # on an already-succeeded attempt, so re-entering is safe.
    if application.status not in {"prepared", "awaiting_consent", "submitting"}:
        return {"skipped": f"status {application.status} is not ready to submit"}
    return submit_application(db, application)


def _handle_app_submit_verify(db: Session, job: Job, now) -> dict[str, Any]:
    """Resume a browser run that parked on an email verification or a code."""
    from .applications.submission import resume_verification
    from .models import ApplicationSubmission

    submission_id = job.payload.get("submission_id") or ""
    submission = db.get(ApplicationSubmission, submission_id)
    if submission is None:
        return {"skipped": "submission not found", "submission_id": submission_id}
    if submission.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if submission.status != "needs_verification":
        return {"skipped": f"submission is {submission.status}, not parked"}
    return resume_verification(db, submission)


HANDLERS = {
    KIND_CRM_DISCOVERY: _handle_crm_discovery,
    KIND_CRM_DISCOVERY_ONE: _handle_crm_discovery_one,
    KIND_CRM_DISCOVERY_IMPORT: _handle_crm_discovery_import,
    KIND_OUTREACH_TICK: _handle_outreach_tick,
    KIND_OUTREACH_SEND: _handle_outreach_send,
    KIND_OUTREACH_FOLLOW_UP: _handle_outreach_follow_up,
    KIND_AGENT_TICK: _handle_agent_tick,
    KIND_MAILBOX_SYNC: _handle_mailbox_sync,
    KIND_MAILBOX_SYNC_ONE: _handle_mailbox_sync_one,
    KIND_MAILBOX_BACKFILL: _handle_mailbox_backfill,
    KIND_REPLY_DRAFT: _handle_reply_draft,
    KIND_JOBS_PARSE: _handle_jobs_parse,
    KIND_JOBS_MATCH: _handle_jobs_match,
    KIND_JOBS_MATCH_ONE: _handle_jobs_match_one,
    KIND_JOBS_DISCOVER_ONE: _handle_jobs_discover_one,
    KIND_JOBS_SCRAPE: _handle_jobs_scrape,
    KIND_JOBS_SCRAPE_ONE: _handle_jobs_scrape_one,
    KIND_JOBS_FORMSPEC_ONE: _handle_jobs_formspec_one,
    KIND_INTAKE_TURN: _handle_intake_turn,
    KIND_INTAKE_EXTRACT: _handle_intake_extract,
    KIND_INTAKE_ATTACHMENT: _handle_intake_attachment,
    KIND_INTAKE_REFERRAL: _handle_intake_referral,
    KIND_INTAKE_MATCH_TEXT: _handle_intake_match_text,
    KIND_DOC_PARSE: _handle_doc_parse,
    KIND_APP_PACKAGE: _handle_app_package,
    KIND_APP_SUBMIT: _handle_app_submit,
    KIND_APP_SUBMIT_VERIFY: _handle_app_submit_verify,
    KIND_APPLY_LINK: _handle_apply_link,
    KIND_PROFILE_INGEST: _handle_profile_ingest,
    KIND_PROFILE_RESUME: _handle_profile_resume,
    KIND_THREAD_NOTIFY: _handle_thread_notify,
    KIND_LINKEDIN_SCRAPE: _handle_linkedin_scrape,
    KIND_CONNECTOR_INDEX: _handle_connector_index,
}


def _record_failure(db: Session, job_id: str, error: str, *, now) -> None:
    """Throw the failed handler's partial work away, then record the failure.

    The rollback is the whole point, and two separate bugs lived in its
    absence. A handler whose flush raised (an IntegrityError, say) left the
    session poisoned, so the very next statement raised PendingRollbackError
    out of the worker loop and killed the process -- and because nothing had
    committed, ``attempts`` never incremented, so the job could never park and
    the supervisor crash-looped on it forever, with every other org's queue
    stuck behind it. And a handler that stamps "already done" before the step
    that raised -- profile.ingest writes ``profile_ingest_checked_at`` and then
    texts the candidate -- had that stamp committed anyway, so the retry
    skipped as ``recently_checked``: the paid profile was bought and nobody was
    ever told.

    The job is re-read rather than reused because the rollback expires it; it
    is guaranteed to still be there because ``process_job`` commits it before
    the handler runs.
    """
    db.rollback()
    job = db.get(Job, job_id)
    if job is None:  # deleted underneath us; there is nothing left to record
        return
    mark_failed(job, error, now=now)
    db.commit()


def process_job(db: Session, job: Job, *, now=None) -> None:
    """Run one job and commit it: the work and the status land together.

    One job is the transaction, never a batch. When a whole batch shared one
    commit, a worker killed on job seventeen rolled the sixteen before it back
    to ``queued attempts=0`` while their external effects stood -- the mail was
    already handed to Graph -- so the restart re-sent to every candidate in the
    lost batch.
    """
    now = now or utcnow()
    # The row has to be durable before its handler runs: the failure path below
    # rolls the session back, and an uncommitted job would be discarded along
    # with the work it failed at, leaving nothing to record the failure on. In
    # the worker this commit is also what makes the batch's claim durable,
    # which is the only reason recover_stale_locks has anything to find.
    db.commit()
    # Read out before the handler runs. A flush that raised leaves the instance
    # expired on a transaction that refuses to reload it, so naming job.id in
    # the log line below would itself raise PendingRollbackError out of the
    # except block -- the exact failure that block exists to contain.
    job_id, job_kind = job.id, job.kind
    handler = HANDLERS.get(job_kind)
    if handler is None:
        mark_failed(job, f"unknown kind {job_kind}", now=now)
        db.commit()
        return
    try:
        result = handler(db, job, now) or {}
    except Exception as exc:
        logger.exception("job %s (%s) failed", job_id, job_kind)
        _record_failure(db, job_id, str(exc), now=now)
        return
    if result.get("defer"):
        settings = get_settings()
        job.status = "queued"
        job.locked_at = None
        job.locked_by = ""
        # A handler that knows exactly when the work becomes possible says so
        # (quiet-hours deferral names the window's opening); otherwise the
        # generic short backoff applies.
        job.run_at = result.get("defer_until") or (
            now + timedelta(seconds=max(settings.worker_tick_seconds, 30))
        )
        db.commit()
        return
    mark_succeeded(job, now=now)
    db.commit()


# A texter is waiting on these, so they get their own small claim ahead of the
# general batch. Ordering inside one claim was not enough: a batch runs
# serially, so a turn behind nineteen mailbox syncs waits for all nineteen --
# measured at ~90 seconds between "Hello" and the greeting on a live thread.
INTERACTIVE_BATCH = 5


def process_batch(
    db: Session,
    *,
    worker: str | None = None,
    now=None,
    only: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
) -> int:
    """Run a batch. ``only`` restricts this process to certain kinds;
    ``exclude`` keeps it away from kinds another process owns.

    Claiming interactive work first is not enough on its own, and the live
    thread proved it: the worker was already ten minutes into a batch of
    `jobs.parse` calls when a text arrived, so the reply landed 284 seconds
    later, at the exact moment the batch drained. Nothing inside one process
    can preempt work already running -- which is why intake gets a process of
    its own, and this argument is how it is told so. ``exclude`` is the other
    half of that design: a dedicated worker only guarantees latency if the
    general one cannot claim the same jobs into its own serial batch first.
    """
    now = now or utcnow()
    recover_stale_locks(db, now=now)
    me = worker or worker_id()

    claims: tuple[tuple[tuple[str, ...] | None, int | None], ...] = (
        ((only, INTERACTIVE_BATCH),)
        if only is not None
        else ((INTERACTIVE_KINDS, INTERACTIVE_BATCH), (None, None))
    )

    done = 0
    for kinds, limit in claims:
        jobs = claim(db, worker_id=me, now=now, kinds=kinds, limit=limit, exclude=exclude)
        # The claim -- and any lock recovery above it -- is committed before the
        # first handler runs, not after the last one. A worker killed mid-batch
        # has to leave its rows visibly `running` for recover_stale_locks to
        # requeue; while the batch was one transaction those rows rolled back to
        # `queued attempts=0` instead, which is what made that function dead
        # code. Each job then commits itself, so a crash costs at most the one
        # job in flight.
        db.commit()
        for job in jobs:
            process_job(db, job, now=now)
        done += len(jobs)
    return done


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


def run_loop(
    *,
    poll_seconds: float | None = None,
    only: tuple[str, ...] | None = None,
    exclude: tuple[str, ...] | None = None,
) -> None:
    from .db import session_scope

    settings = get_settings()
    delay = settings.worker_poll_seconds if poll_seconds is None else poll_seconds
    wid = worker_id()
    scope = ",".join(only) if only else "all kinds"
    if exclude:
        scope += " except " + ",".join(exclude)
    logger.info("worker %s polling every %.1fs (%s)", wid, delay, scope)
    while True:
        try:
            with session_scope() as db:
                # Only the general worker schedules the recurring ticks. A
                # second process doing it too would double every sweep.
                # `exclude` does not change that: the excluding process is
                # still the general worker.
                if only is None:
                    ensure_ticks(db)
                n = process_batch(db, worker=wid, only=only, exclude=exclude)
        except Exception:
            # A dropped connection, a poisoned session, a bug in a handler's
            # own cleanup: none of it is a reason to take the process down and
            # block every org's queue behind a crash-looping supervisor. Every
            # job that finished is already committed on its own, so backing off
            # and claiming again is a complete recovery.
            logger.exception("worker %s: loop iteration failed, backing off", wid)
            time.sleep(max(delay, 1.0))
            continue
        if n == 0:
            time.sleep(max(delay, 0.2))

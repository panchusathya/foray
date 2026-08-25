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
    KIND_CRM_DISCOVERY,
    KIND_CRM_DISCOVERY_IMPORT,
    KIND_CRM_DISCOVERY_ONE,
    KIND_DOC_PARSE,
    KIND_INTAKE_EXTRACT,
    KIND_INTAKE_TURN,
    KIND_JOBS_DISCOVER_ONE,
    KIND_JOBS_MATCH,
    KIND_JOBS_MATCH_ONE,
    KIND_JOBS_PARSE,
    KIND_JOBS_SCRAPE,
    KIND_JOBS_SCRAPE_ONE,
    KIND_LINKEDIN_SCRAPE,
    KIND_MAILBOX_BACKFILL,
    KIND_MAILBOX_SYNC,
    KIND_MAILBOX_SYNC_ONE,
    KIND_OUTREACH_SEND,
    KIND_OUTREACH_TICK,
    KIND_PROFILE_INGEST,
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
    from .messaging.conversation import ACTION_DEFERRED, run_turn
    from .messaging.linq import maybe_start_typing
    from .models import IntakeThread

    thread_id = job.payload.get("thread_id") or ""
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        return {"skipped": "thread not found", "thread_id": thread_id}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    maybe_start_typing(thread)
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
    from .docgen.parse import extract_pdf_text, parse_resume_text
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
        doc.text_extracted = extract_pdf_text(data)

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
            promoted["thread"] = _resume_settles_thread(db, doc=doc, candidate=candidate)

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
    notify_candidate(
        db,
        org_id=job.org_id,
        candidate_id=application.candidate_id,
        event=EVENT_PACKAGE_READY,
        subject_id=application.id,
    )
    # Whatever the form asks that we still cannot answer, asked now -- with the
    # role in hand. Onboarding no longer collects comp, notice period or work
    # auth in the abstract; this is where they are worth a message. After the
    # package notification on purpose: the candidate sees the role first, then
    # the two questions it raised.
    from .applications.followups import ask_for_missing

    result["followup"] = ask_for_missing(db, application)
    return result


def _handle_thread_notify(db: Session, job: Job, now) -> dict[str, Any]:
    from .messaging.notify import handle_notify

    return handle_notify(db, job, now)


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

    Two sources, in cost order: the browser scrape, then the paid provider if
    it misses. See ``enrich.profile_sources``.
    """
    from .enrich.linkedin_scrape import ScrapeError, scrape_profile
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
    try:
        profile = scrape_profile(url)
    except ScrapeError as exc:
        logger.warning("linkedin scrape failed for thread %s: %s", thread.id, exc)
        failure = str(exc)
    except Exception as exc:
        logger.exception("linkedin scrape crashed for thread %s", thread.id)
        failure = str(exc)

    if profile is None:
        # Free first, paid second. The scrape hitting an auth wall says
        # nothing about whether this person exists, so spend a lookup rather
        # than drop the candidate into a question batch they did not need.
        profile, paid_error = _paid_profile_fallback(db, thread=thread, url=url)
        if profile is None:
            update_flow_meta(thread, error=(failure or paid_error)[:400], scrape=None)

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
    KIND_JOBS_DISCOVER_ONE: _handle_jobs_discover_one,
    KIND_JOBS_SCRAPE: _handle_jobs_scrape,
    KIND_JOBS_SCRAPE_ONE: _handle_jobs_scrape_one,
    KIND_INTAKE_TURN: _handle_intake_turn,
    KIND_INTAKE_EXTRACT: _handle_intake_extract,
    KIND_DOC_PARSE: _handle_doc_parse,
    KIND_APP_PACKAGE: _handle_app_package,
    KIND_PROFILE_INGEST: _handle_profile_ingest,
    KIND_THREAD_NOTIFY: _handle_thread_notify,
    KIND_LINKEDIN_SCRAPE: _handle_linkedin_scrape,
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
        # A handler that knows exactly when the work becomes possible says so
        # (quiet-hours deferral names the window's opening); otherwise the
        # generic short backoff applies.
        job.run_at = result.get("defer_until") or (
            now + timedelta(seconds=max(settings.worker_tick_seconds, 30))
        )
        return
    mark_succeeded(job, now=now)


# A texter is waiting on these, so they get their own small claim ahead of the
# general batch. Ordering inside one claim was not enough: a batch runs
# serially, so a turn behind nineteen mailbox syncs waits for all nineteen --
# measured at ~90 seconds between "Hello" and the greeting on a live thread.
INTERACTIVE_BATCH = 5


def process_batch(db: Session, *, worker: str | None = None, now=None) -> int:
    now = now or utcnow()
    recover_stale_locks(db, now=now)
    me = worker or worker_id()

    done = 0
    for kinds, limit in ((INTERACTIVE_KINDS, INTERACTIVE_BATCH), (None, None)):
        jobs = claim(db, worker_id=me, now=now, kinds=kinds, limit=limit)
        for job in jobs:
            process_job(db, job, now=now)
        if jobs:
            db.flush()
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

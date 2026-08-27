"""Durable job queue.

Postgres workers claim with ``FOR UPDATE SKIP LOCKED``. SQLite has no skip-locked,
so tests (and ``make worker`` on the demo file) just take the next queued row —
one process at a time is the contract there.

Enqueue is idempotent on ``(org_id, idempotency_key)``. Send jobs keep that key
forever so a step cannot be enqueued twice; ticks use a timestamped key so the
next tick can be scheduled after the current one succeeds. The one exception is
a row that has burned every attempt -- see ``enqueue``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, select
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
KIND_JOBS_PARSE = "jobs.parse"
KIND_JOBS_MATCH = "jobs.match"
KIND_JOBS_MATCH_ONE = "jobs.match.one"
KIND_JOBS_SCRAPE = "jobs.scrape"
KIND_JOBS_SCRAPE_ONE = "jobs.scrape.one"
KIND_JOBS_DISCOVER_ONE = "jobs.discover.one"
# Deriving a FormSpec from one unrecognised posting's own page (a browser page
# view, read-only). Enqueued by the carding gate when a bar-passing match hits
# a board nobody mapped -- never on ensure_ticks, because the book is far
# larger than the set anyone is ever shown and each derivation is a real page
# view through the proxy. Idempotent per posting; a failed row is revived by
# the next candidate match, which is the retry story.
KIND_JOBS_FORMSPEC_ONE = "jobs.formspec.one"

# Contact discovery mirrors the sync split: a per-org dispatcher fans out
# one job per mailbox. It is never added to ensure_ticks -- reading a year
# of mail metadata is a deliberate, consent-shaped action, like
# mailbox.backfill, not something a worker should start on its own.
KIND_CRM_DISCOVERY = "crm.discovery"
KIND_CRM_DISCOVERY_ONE = "crm.discovery.one"
KIND_CRM_DISCOVERY_IMPORT = "crm.discovery.import"

# One turn of a text intake thread. The web widget calls run_turn inline --
# somebody is watching a typing indicator -- but a channel whose inbound
# arrives by webhook must answer the vendor immediately and do the work
# here, because extraction and delivery are both too slow for a webhook
# response. Idempotent on the inbound message id, so a redelivered webhook
# resolves to the job that already ran.
KIND_INTAKE_TURN = "intake.thread.turn"

# Browser scrape of a LinkedIn URL captured on a text thread. Slow and
# failure-prone (auth wall, missing cookies), so it is a job rather than
# inline in the turn: the turn holds, this job writes the scrape onto the
# thread, then re-enqueues a turn to send the confirm message.
KIND_LINKEDIN_SCRAPE = "intake.linkedin.scrape"

# One targeted index of a candidate-connected app (Gmail / Outlook / GitHub):
# find the resume, derive the profile, write the thread's scrape slot, resume
# the conversation. Enqueued by the OAuth callback with a fresh run id per
# connect — never on ensure_ticks, because a candidate grant is indexed when
# they connect it, not on a loop.
KIND_CONNECTOR_INDEX = "intake.connector.index"

# Reading a finished intake call into a preference record. A job rather than
# inline work in the webhook: the provider times out at 10s and retries three
# times, so an extraction on the request path turns one slow model call into
# four copies of the same conversation. Never added to ensure_ticks -- it is
# enqueued by a call ending, not on a schedule.
KIND_INTAKE_EXTRACT = "intake.extract"

# Parsing one uploaded candidate document (pypdf + one model call). Enqueued
# by an upload landing; never on ensure_ticks.
KIND_DOC_PARSE = "docs.parse"

# Downloading the files attached to one inbound message and, when one is a
# resume, storing it for docs.parse. A job rather than webhook work for the
# same reason turns are: the download is slow and the vendor is waiting.
# Idempotent on the inbound message id. Never on ensure_ticks.
KIND_INTAKE_ATTACHMENT = "intake.attachment"

# Answering one iMessage group chat: fetch the participants, record a referral
# per newcomer, send the single reply, leave. A job rather than webhook work
# for the same reason attachments are -- three network calls with the vendor
# waiting on a 200. Idempotent on the group thread, which is closed on the way
# out so a replay finds nothing to do.
KIND_INTAKE_REFERRAL = "intake.referral"

# Fetching one LinkedIn profile through the paid provider and promoting it
# onto the CRM. Worker-only by design: the webhook that receives a URL
# enqueues and returns, so a public request path never spends money.
KIND_PROFILE_INGEST = "profile.ingest"

# Refreshing the candidate's ONE profile-level ATS resume (docgen.profile)
# after a promotion lands new facts. Enqueued by the events that change what
# the resume would say — a paid profile fetch, a parsed upload, an identity
# link — never on ensure_ticks. Failures are recorded on the document row,
# not raised: the next promote event is the retry.
KIND_PROFILE_RESUME = "profile.resume"

# One proactive message to one candidate thread (profile ready, cards, a
# package to review, a status change). Event-keyed idempotency so a rescore
# firing twice cannot double-text; every cadence gate applies inside the
# handler. Never on ensure_ticks -- notifications are enqueued by the events
# they announce.
KIND_THREAD_NOTIFY = "thread.notify"

# Building one application package: tailored resume PDF, cover letter, form
# answers. User-initiated ("apply to 2", a swipe, a recruiter click); never on
# ensure_ticks. Idempotency keys carry a timestamp because a rebuild is a
# legitimate second request -- the per-application open-job guard in
# applications.service is what collapses double-clicks.
KIND_APP_PACKAGE = "application.package"

# One attempt to actually send one application. Never on ensure_ticks: an
# automated submission starts from a recorded per-application yes, never from a
# timer. The idempotency key carries the attempt number, so a retry after a
# failure is a new job and a redelivered one is not.
KIND_APP_SUBMIT = "application.submit"
# The second leg of a parked browser run: the form needed an email verification
# or a one-time code, the alias mailbox received it, and the run resumes.
KIND_APP_SUBMIT_VERIFY = "application.submit.verify"

OPEN_STATUSES = ("queued", "running")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _revive(
    job: Job,
    *,
    payload: dict[str, Any] | None,
    run_at: datetime | None,
    max_attempts: int | None,
) -> Job:
    """Put a parked job back on the queue for a caller that asked again.

    ``last_error`` is deliberately left in place: it is the only record of why
    the job parked, and the next attempt overwrites it anyway.
    """
    job.status = "queued"
    job.attempts = 0
    job.run_at = run_at or utcnow()
    job.locked_at = None
    job.locked_by = ""
    job.finished_at = None
    if payload is not None:
        job.payload = payload
    if max_attempts:
        job.max_attempts = max_attempts
    return job


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
        # A permanently failed row is the one status that must not be handed
        # back as-is. Send keys are permanent and the backoff is 2/4/8/16/32s,
        # so a half-minute Graph outage burns all five attempts and parks the
        # job forever -- after which every outreach tick "re-enqueues" the same
        # dead row and cheerfully reports queued_sends: 1 while the enrollment
        # never moves again.
        #
        # Reviving only `failed` is what keeps the double-send guarantee
        # intact. queued and running mean an attempt is pending or in flight,
        # and a second row would be a second send; succeeded means the effect
        # landed, and re-running it is precisely the double-send the key
        # exists to prevent. failed means the queue has already given up and
        # nothing further will happen on this row, so a later caller asking
        # for the work again is a new request, not a duplicate of a live one.
        if existing.status == "failed":
            return _revive(
                existing, payload=payload, run_at=run_at, max_attempts=max_attempts
            )
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


# Work with somebody waiting at the other end of it. Ordering these first
# inside a claim is not enough on its own: a batch is processed serially, so a
# turn queued behind nineteen mailbox syncs waits for all nineteen. The worker
# claims these separately for that reason.
INTERACTIVE_KINDS = (KIND_INTAKE_TURN, KIND_LINKEDIN_SCRAPE, KIND_CONNECTOR_INDEX)


def claim(
    db: Session,
    *,
    worker_id: str,
    limit: int | None = None,
    now: datetime | None = None,
    kinds: tuple[str, ...] | None = None,
) -> list[Job]:
    settings = get_settings()
    now = now or utcnow()
    batch = limit if limit is not None else settings.worker_batch_size
    priority = case(
        (Job.kind.in_(INTERACTIVE_KINDS), 0),
        else_=1,
    )
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_at <= now)
        .order_by(priority, Job.run_at, Job.created_at)
        .limit(max(batch, 1))
    )
    if kinds is not None:
        stmt = stmt.where(Job.kind.in_(kinds))
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

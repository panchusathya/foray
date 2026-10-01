"""In-flight sequence email: the listing the Outbox page reads, cancel, and undo.

The send gate lives in ``sequencer.sending_hold`` / ``process_enrollment``.
This module is the operator view of the same predicates, plus the two verbs
that take a row out of that view without sending it -- and the one that puts
a stopped row back, because "stop" turned out to mean "not at that time"
about as often as it meant "not at all".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..jobs import KIND_OUTREACH_SEND, OPEN_STATUSES, cancel_job
from ..models import (
    Enrollment,
    Job,
    MailboxConnection,
    Org,
    Sequence,
    record_event,
    utcnow,
)
from .sequencer import (
    _aware,
    _blocked_until,
    _effective_daily_cap,
    _finish_enrollment,
    _sent_today,
    open_send_jobs,
    recent_human_replies,
    schedule_step_at,
    sending_hold,
    spacing_wait,
)

HELD_STATES = frozenset({"sending", "due", "held"})

# The one stopped_reason a re-enrol will bring back. Every other stop is the
# system's verdict on the person -- they replied, bounced, are suppressed, or
# have no address -- and re-enrolling would not change the fact that stopped
# them. This one is the operator's verdict on the *timing*, and timing is
# the thing a re-enrol lets them change.
STOPPED_BY_USER = "cancelled_by_user"
STATE_CANCELLED = "cancelled"

# When a re-enrolled step goes out. ``next_slot`` is the default because it is
# what "not at 3pm" usually means: the step's own hour, next time it comes
# round, with no delay counted -- the delay was already served before the stop.
WHEN_NOW = "now"
WHEN_NEXT_SLOT = "next_slot"
WHEN_AT = "at"


@dataclass(frozen=True)
class InFlightItem:
    enrollment_id: str
    candidate_id: str
    candidate_name: str
    candidate_email: str
    sequence_id: str
    sequence_name: str
    step: int
    mailbox: str
    due_at: datetime | None
    state: str
    reason: str | None
    # Set only on cancelled rows: when the operator stopped it. Nothing else
    # about a cancelled row differs from a live one, so the same shape serves
    # both listings and the page can render them in one grid.
    cancelled_at: datetime | None = None
    # Place in the org's send line (1 goes next), counting rows waiting on a
    # full mailbox. None for anything not yet queued.
    rank: int | None = None


@dataclass
class CancelResult:
    skipped: int = 0
    stopped: int = 0
    already_sending: int = 0
    not_found: int = 0


def _row_state(
    *,
    job: Job | None,
    enrollment: Enrollment,
    hold: str | None,
    remaining_cap: int,
    now: datetime,
) -> tuple[str, str | None]:
    """State from the same predicates the sender uses, not from run_at alone.

    A pause must read as ``held`` immediately, even before the worker bumps
    ``run_at`` five minutes out. Overdue enrolments behind a paused sequence
    (no job — ``due_enrollments`` never queued them) are held too.
    """
    if job is not None and job.status == "running":
        return "sending", None
    if hold:
        return "held", hold

    job_run_at = _aware(job.run_at) if job is not None else None
    next_send = _aware(enrollment.next_send_at)
    due_now = False
    if job is not None and job.status == "queued":
        due_now = job_run_at is not None and job_run_at <= now
    elif next_send is not None:
        due_now = next_send <= now

    if remaining_cap == 0 and (job is not None or due_now):
        return "held", "daily_cap"
    if job is not None and job.status == "queued":
        return "due", None
    if due_now:
        return "due", None
    return "scheduled", None


def _mailbox_address(enrollment: Enrollment, addresses: dict[str, str]) -> str:
    if enrollment.mailbox_id:
        return addresses.get(enrollment.mailbox_id, "") or (
            enrollment.sequence.from_email if enrollment.sequence is not None else ""
        )
    if enrollment.sequence is not None:
        return enrollment.sequence.from_email or ""
    return ""


def _mailbox_addresses(db: Session, enrollments: list[Enrollment]) -> dict[str, str]:
    mailbox_ids = {row.mailbox_id for row in enrollments if row.mailbox_id}
    if not mailbox_ids:
        return {}
    return {
        row.id: row.address
        for row in db.scalars(
            select(MailboxConnection).where(MailboxConnection.id.in_(mailbox_ids))
        ).all()
    }


def collect_in_flight(
    db: Session,
    *,
    org_id: str,
    now: datetime | None = None,
    sequence_id: str | None = None,
) -> list[InFlightItem]:
    now = now or utcnow()
    org = db.get(Org, org_id)
    remaining_cap = max(0, _effective_daily_cap(db, org_id) - _sent_today(db, org_id, now))

    jobs = list(
        db.scalars(
            select(Job).where(
                Job.org_id == org_id,
                Job.kind == KIND_OUTREACH_SEND,
                Job.status.in_(OPEN_STATUSES),
            )
        ).all()
    )
    job_by_enrollment: dict[str, Job] = {}
    for job in jobs:
        enrollment_id = (job.payload or {}).get("enrollment_id")
        if isinstance(enrollment_id, str) and enrollment_id:
            job_by_enrollment[enrollment_id] = job

    job_ids = list(job_by_enrollment)
    query = (
        select(Enrollment)
        .join(Sequence, Sequence.id == Enrollment.sequence_id)
        .where(
            Sequence.org_id == org_id,
            Enrollment.status == "active",
            or_(
                Enrollment.next_send_at.is_not(None),
                Enrollment.id.in_(job_ids) if job_ids else false(),
            ),
        )
        .options(
            selectinload(Enrollment.candidate),
            selectinload(Enrollment.sequence).selectinload(Sequence.steps),
        )
    )
    if sequence_id:
        query = query.where(Enrollment.sequence_id == sequence_id)
    enrollments = list(db.scalars(query).all())
    addresses = _mailbox_addresses(db, enrollments)

    # The whole org's line, whatever this view filters to, so a rank means
    # the same thing on every page. The estimate counts only rows that can
    # send: one waiting on a full mailbox does not use up a gap.
    line = open_send_jobs(db, org_id)
    ranks = {job.id: index for index, job in enumerate(line, start=1)}
    settings = get_settings()
    gap = timedelta(
        seconds=(
            max(settings.outreach_send_spacing_seconds, 0)
            + max(settings.outreach_send_spacing_max_seconds, settings.outreach_send_spacing_seconds)
        )
        / 2
    )
    head = spacing_wait(db, org_id, now) or now
    estimates: dict[str, datetime] = {}
    for job in line:
        until = _blocked_until(job)
        if until is None or until <= now:
            estimates[job.id] = head + gap * len(estimates)

    items: list[InFlightItem] = []
    for enrollment in enrollments:
        sequence = enrollment.sequence
        candidate = enrollment.candidate
        job = job_by_enrollment.get(enrollment.id)
        hold = sending_hold(db, sequence, now=now, org=org)
        state, reason = _row_state(
            job=job,
            enrollment=enrollment,
            hold=hold,
            remaining_cap=remaining_cap,
            now=now,
        )
        due_at = _aware(enrollment.next_send_at)
        if job is not None and job.status == "queued":
            until = _blocked_until(job)
            if state == "due" and until is not None and until > now:
                state, reason, due_at = "held", "mailbox_full", until
            elif state == "due":
                due_at = estimates.get(job.id, _aware(job.run_at))
            else:
                due_at = _aware(job.run_at)
        elif job is not None:
            due_at = _aware(job.run_at)
        items.append(
            InFlightItem(
                enrollment_id=enrollment.id,
                candidate_id=enrollment.candidate_id,
                candidate_name=(candidate.full_name if candidate is not None else ""),
                candidate_email=(
                    (candidate.primary_email or "") if candidate is not None else ""
                ),
                sequence_id=enrollment.sequence_id,
                sequence_name=sequence.name if sequence is not None else "",
                step=enrollment.current_step,
                mailbox=_mailbox_address(enrollment, addresses),
                due_at=due_at,
                state=state,
                reason=reason,
                rank=ranks.get(job.id) if job is not None else None,
            )
        )

    # The line first, in rank order -- that is the order they will go, and
    # the order a move up or down acts on. Everything else by its time.
    items.sort(
        key=lambda row: (
            row.rank is None,
            row.rank or 0,
            _aware(row.due_at) or datetime(9999, 12, 31, tzinfo=UTC),
            row.candidate_name.lower(),
            row.enrollment_id,
        )
    )
    return items


def _cancelled_query(org_id: str, sequence_id: str | None):
    query = (
        select(Enrollment)
        .join(Sequence, Sequence.id == Enrollment.sequence_id)
        .where(
            Sequence.org_id == org_id,
            Enrollment.status == "stopped",
            Enrollment.stopped_reason == STOPPED_BY_USER,
        )
    )
    if sequence_id:
        query = query.where(Enrollment.sequence_id == sequence_id)
    return query


def collect_cancelled(
    db: Session, *, org_id: str, sequence_id: str | None = None
) -> list[InFlightItem]:
    """Enrolments the operator stopped from the outbox, newest stop first.

    These are the rows ``reenroll_in_flight`` can bring back. ``step`` is the
    step that was about to send when it was stopped -- ``_finish_enrollment``
    leaves ``current_step`` alone -- so it is also where a re-enrol resumes.
    """
    enrollments = list(
        db.scalars(
            _cancelled_query(org_id, sequence_id).options(
                selectinload(Enrollment.candidate),
                selectinload(Enrollment.sequence),
            )
        ).all()
    )
    addresses = _mailbox_addresses(db, enrollments)
    items = [
        InFlightItem(
            enrollment_id=enrollment.id,
            candidate_id=enrollment.candidate_id,
            candidate_name=(
                enrollment.candidate.full_name if enrollment.candidate is not None else ""
            ),
            candidate_email=(
                (enrollment.candidate.primary_email or "")
                if enrollment.candidate is not None
                else ""
            ),
            sequence_id=enrollment.sequence_id,
            sequence_name=(enrollment.sequence.name if enrollment.sequence is not None else ""),
            step=enrollment.current_step,
            mailbox=_mailbox_address(enrollment, addresses),
            due_at=None,
            state=STATE_CANCELLED,
            reason=None,
            cancelled_at=_aware(enrollment.updated_at),
        )
        for enrollment in enrollments
    ]
    items.sort(
        key=lambda row: (
            -(row.cancelled_at or datetime(1970, 1, 1, tzinfo=UTC)).timestamp(),
            row.candidate_name.lower(),
            row.enrollment_id,
        )
    )
    return items


def cancelled_count(db: Session, *, org_id: str, sequence_id: str | None = None) -> int:
    """How many stopped rows a re-enrol could bring back. For the page's hint."""
    subquery = _cancelled_query(org_id, sequence_id).subquery()
    return int(db.scalar(select(func.count()).select_from(subquery)) or 0)


def list_in_flight(
    db: Session,
    *,
    org_id: str,
    now: datetime | None = None,
    state: str | None = None,
    sequence_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[int, list[InFlightItem]]:
    # Cancelled rows are a population of their own, not a state of the live
    # one: nothing about them is about to send. They share the listing so the
    # page has one grid, but the default view stays "what is going out".
    if state == STATE_CANCELLED:
        rows = collect_cancelled(db, org_id=org_id, sequence_id=sequence_id)
    else:
        rows = collect_in_flight(db, org_id=org_id, now=now, sequence_id=sequence_id)
        if state:
            rows = [row for row in rows if row.state == state]
    return len(rows), rows[offset : offset + limit]


def held_count(db: Session, *, org_id: str, now: datetime | None = None) -> int:
    rows = collect_in_flight(db, org_id=org_id, now=now)
    return sum(1 for row in rows if row.state in HELD_STATES)


def _skip_step(enrollment: Enrollment, *, now: datetime) -> None:
    steps = sorted(enrollment.sequence.steps, key=lambda step: step.step_order)
    enrollment.current_step += 1
    enrollment.snoozed_until = None
    if enrollment.current_step >= len(steps):
        _finish_enrollment(enrollment, "completed")
        return
    next_step = steps[enrollment.current_step]
    enrollment.next_send_at = schedule_step_at(next_step, base=now)


def cancel_in_flight(
    db: Session,
    *,
    org_id: str,
    enrollment_ids: list[str],
    action: str,
    now: datetime | None = None,
) -> CancelResult:
    now = now or utcnow()
    result = CancelResult()
    if not enrollment_ids:
        return result

    enrollments = {
        row.id: row
        for row in db.scalars(
            select(Enrollment)
            .join(Sequence, Sequence.id == Enrollment.sequence_id)
            .where(Sequence.org_id == org_id, Enrollment.id.in_(enrollment_ids))
            .options(selectinload(Enrollment.sequence).selectinload(Sequence.steps))
        ).all()
    }

    jobs = list(
        db.scalars(
            select(Job).where(
                Job.org_id == org_id,
                Job.kind == KIND_OUTREACH_SEND,
                Job.status.in_(OPEN_STATUSES),
            )
        ).all()
    )
    jobs_by_enrollment: dict[str, list[Job]] = {}
    for job in jobs:
        enrollment_id = (job.payload or {}).get("enrollment_id")
        if isinstance(enrollment_id, str) and enrollment_id:
            jobs_by_enrollment.setdefault(enrollment_id, []).append(job)

    seen: set[str] = set()
    for enrollment_id in enrollment_ids:
        if enrollment_id in seen:
            continue
        seen.add(enrollment_id)
        enrollment = enrollments.get(enrollment_id)
        open_jobs = jobs_by_enrollment.get(enrollment_id, [])
        if (
            enrollment is None
            or enrollment.status != "active"
            or (not open_jobs and enrollment.next_send_at is None)
        ):
            result.not_found += 1
            continue
        if any(job.status == "running" for job in open_jobs):
            result.already_sending += 1
            continue
        for job in open_jobs:
            if job.status == "queued":
                cancel_job(job, now=now)
        if action == "stop":
            _finish_enrollment(enrollment, "stopped", "cancelled_by_user")
            result.stopped += 1
        else:
            _skip_step(enrollment, now=now)
            result.skipped += 1
    db.flush()
    return result


# --------------------------------------------------------------------------
# re-enrol: undo a stop
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReenrollBlocked:
    """Somebody who answered us by hand inside the quiet period.

    Reported rather than silently dropped, the same way ``/enroll`` does it:
    the operator decides whether the conversation they are in should also get
    the automated step, and says so with ``force``.
    """

    candidate_id: str
    full_name: str
    replied_at: datetime | None
    reply_intent: str


@dataclass
class ReenrollResult:
    resumed: int = 0
    # A stop taken after the last step had already gone out has nothing to
    # resume. Rare -- the outbox only offers stop on a row with a step to send
    # -- but a stop-by-API or a since-shortened sequence can produce one.
    completed: int = 0
    not_found: int = 0
    replied: list[ReenrollBlocked] = field(default_factory=list)
    # The rows now active again, in request order, for a caller that wants to
    # send them in the same request the way ``/enroll`` does with send_now.
    enrollments: list[Enrollment] = field(default_factory=list)


def _drop_cancelled_jobs(db: Session, *, org_id: str, enrollment_id: str, step_id: str) -> None:
    """Delete the cancelled send row for this step so the tick can queue a new one.

    ``cancel_job`` keeps the row on purpose: ``enqueue`` hands a cancelled key
    back as-is, which is what stops a tick from re-arming a step the operator
    just stopped. Re-enrolling is the one caller that wants exactly that
    re-arming -- and wants it on the tick's own terms. Deleting the row rather
    than flipping it back to ``queued`` with a far-off ``run_at`` means a
    re-enrol for Thursday shows as *scheduled* until Thursday, exactly like a
    fresh enrolment, instead of as a queued job the listing reads as due.
    """
    key = f"{KIND_OUTREACH_SEND}:{enrollment_id}:{step_id}"
    for job in db.scalars(
        select(Job).where(
            Job.org_id == org_id,
            Job.idempotency_key == key,
            Job.status == "cancelled",
        )
    ).all():
        db.delete(job)


def reenroll_in_flight(
    db: Session,
    *,
    org_id: str,
    enrollment_ids: list[str],
    when: str = WHEN_NEXT_SLOT,
    send_at: datetime | None = None,
    force: bool = False,
    now: datetime | None = None,
) -> ReenrollResult:
    """Put enrolments the operator stopped back on the rails, at the stopped step.

    Only ``cancelled_by_user`` rows qualify; anything else the caller names is
    counted in ``not_found`` and left alone, which is also what keeps this
    from ever reviving a reply-stopped enrolment (see ``test_reply_gating``).
    Resuming at ``current_step`` rather than step one is deliberate: the steps
    before it were sent, and the message index would refuse to send them
    twice anyway.

    ``when`` picks the new send time. ``now`` makes the step due immediately.
    ``next_slot`` lands it on the step's own send hour with no delay counted.
    ``at`` uses ``send_at`` as given; a time already past is simply due.
    """
    now = now or utcnow()
    if when == WHEN_AT and send_at is None:
        raise ValueError("send_at is required when when='at'")
    if when not in (WHEN_NOW, WHEN_NEXT_SLOT, WHEN_AT):
        raise ValueError(f"unknown when: {when!r}")

    result = ReenrollResult()
    wanted = list(dict.fromkeys(eid for eid in enrollment_ids if eid))
    if not wanted:
        return result

    rows = {
        row.id: row
        for row in db.scalars(
            _cancelled_query(org_id, None)
            .where(Enrollment.id.in_(wanted))
            .options(
                selectinload(Enrollment.candidate),
                selectinload(Enrollment.sequence).selectinload(Sequence.steps),
            )
        ).all()
    }
    result.not_found = sum(1 for eid in wanted if eid not in rows)
    eligible = [rows[eid] for eid in wanted if eid in rows]
    if not eligible:
        return result

    blocked = (
        {}
        if force
        else recent_human_replies(
            db,
            org_id=org_id,
            candidate_ids=[row.candidate_id for row in eligible],
            now=now,
            days=get_settings().outreach_reply_quiet_days,
        )
    )

    for enrollment in eligible:
        hit = blocked.get(enrollment.candidate_id)
        if hit is not None:
            result.replied.append(
                ReenrollBlocked(
                    candidate_id=enrollment.candidate_id,
                    full_name=(
                        enrollment.candidate.full_name if enrollment.candidate is not None else ""
                    ),
                    replied_at=_aware(hit.sent_at),
                    reply_intent=hit.reply_intent,
                )
            )
            continue

        steps = sorted(enrollment.sequence.steps, key=lambda step: step.step_order)
        if enrollment.current_step >= len(steps):
            _finish_enrollment(enrollment, "completed")
            result.completed += 1
            continue
        step = steps[enrollment.current_step]
        _drop_cancelled_jobs(db, org_id=org_id, enrollment_id=enrollment.id, step_id=step.id)

        if when == WHEN_NOW:
            due = now
        elif when == WHEN_AT:
            due = _aware(send_at)
        else:
            due = schedule_step_at(step, base=now, ignore_delay=True)

        enrollment.status = "active"
        enrollment.stopped_reason = ""
        enrollment.snoozed_until = None
        enrollment.next_send_at = due
        record_event(
            db,
            org_id=org_id,
            type="outreach.reenrolled",
            subject_type="enrollment",
            subject_id=enrollment.id,
            payload={
                "candidate_id": enrollment.candidate_id,
                "sequence_id": enrollment.sequence_id,
                "step": enrollment.current_step,
                "when": when,
                "next_send_at": due.isoformat() if due is not None else None,
            },
        )
        result.resumed += 1
        result.enrollments.append(enrollment)

    db.flush()
    return result

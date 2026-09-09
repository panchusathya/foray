"""In-flight sequence email: the listing the Outbox page reads, and cancel.

The send gate lives in ``sequencer.sending_hold`` / ``process_enrollment``.
This module is the operator view of the same predicates, plus the two verbs
that take a row out of that view without sending it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session, selectinload

from ..jobs import KIND_OUTREACH_SEND, OPEN_STATUSES, cancel_job
from ..models import (
    Enrollment,
    Job,
    MailboxConnection,
    Org,
    Sequence,
    utcnow,
)
from .sequencer import (
    _aware,
    _effective_daily_cap,
    _finish_enrollment,
    _sent_today,
    schedule_step_at,
    sending_hold,
)

HELD_STATES = frozenset({"sending", "due", "held"})


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

    mailbox_ids = {row.mailbox_id for row in enrollments if row.mailbox_id}
    addresses: dict[str, str] = {}
    if mailbox_ids:
        addresses = {
            row.id: row.address
            for row in db.scalars(
                select(MailboxConnection).where(MailboxConnection.id.in_(mailbox_ids))
            ).all()
        }

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
        due_at = _aware(job.run_at) if job is not None else _aware(enrollment.next_send_at)
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
            )
        )
    items.sort(
        key=lambda row: (
            _aware(row.due_at) or datetime(9999, 12, 31, tzinfo=UTC),
            row.candidate_name.lower(),
            row.enrollment_id,
        )
    )
    return items


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

"""Multi-step outreach: enrolment, scheduling, sending, reply handling."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import (
    Candidate,
    Enrollment,
    Message,
    Project,
    ProjectCandidate,
    Sequence,
    SequenceStep,
    record_event,
    utcnow,
)
from ..security import signed_tracking_token
from . import templates
from .personalize import personalize
from .replies import ReplyVerdict, classify_reply
from .sender import OutboundEmail, resolve_sender
from .suppressions import is_suppressed, normalize_email, suppress

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    processed: int = 0
    sent: int = 0
    failed: int = 0
    completed: int = 0
    skipped: int = 0
    messages: list[Message] = field(default_factory=list)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; normalise before comparing."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# enrolment
# --------------------------------------------------------------------------


def enroll_candidates(
    db: Session,
    *,
    sequence: Sequence,
    candidate_ids: list[str],
    project_id: str | None = None,
    start_immediately: bool = True,
    now: datetime | None = None,
) -> list[Enrollment]:
    """Idempotent: re-enrolling an existing candidate returns the existing row."""
    now = now or utcnow()
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    first_delay = steps[0].delay_days if steps else 0

    existing = {
        row.candidate_id: row
        for row in db.scalars(
            select(Enrollment).where(
                Enrollment.sequence_id == sequence.id,
                Enrollment.candidate_id.in_(candidate_ids),
            )
        )
    }

    created: list[Enrollment] = []
    for candidate_id in candidate_ids:
        if candidate_id in existing:
            created.append(existing[candidate_id])
            continue
        candidate = db.get(Candidate, candidate_id)
        if candidate is None or candidate.org_id != sequence.org_id:
            continue
        enrollment = Enrollment(
            sequence_id=sequence.id,
            candidate_id=candidate_id,
            project_id=project_id or sequence.project_id,
            status="active",
            current_step=0,
            next_send_at=(now + timedelta(days=first_delay)) if start_immediately else None,
        )
        db.add(enrollment)
        created.append(enrollment)
        record_event(
            db,
            org_id=sequence.org_id,
            type="outreach.enrolled",
            subject_type="candidate",
            subject_id=candidate_id,
            payload={"sequence_id": sequence.id},
        )
    db.flush()
    return created


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


def build_message_content(
    db: Session,
    *,
    sequence: Sequence,
    step: SequenceStep,
    candidate: Candidate,
    context: dict | None = None,
) -> tuple[str, str, dict]:
    project = db.get(Project, sequence.project_id) if sequence.project_id else None
    if context is None:
        context = personalize(candidate, project, use_llm=sequence.personalize)
    subject = templates.render(step.subject_template, context).strip()
    body = templates.render(step.body_template, context).strip()
    if not subject:
        subject = context.get("subject_hint") or f"Quick note for {context.get('first_name', '')}".strip()
    return subject, body, context


def preview_step(
    db: Session, *, sequence: Sequence, candidate: Candidate, step_order: int = 0
) -> tuple[str, str, dict]:
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    if not steps:
        raise ValueError("sequence has no steps")
    step = next((s for s in steps if s.step_order == step_order), steps[0])
    return build_message_content(db, sequence=sequence, step=step, candidate=candidate)


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def _sent_today(db: Session, org_id: str, now: datetime) -> int:
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        db.scalar(
            select(func.count(Message.id)).where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.status == "sent",
                Message.sent_at >= start_of_day,
            )
        )
        or 0
    )


def _finish_enrollment(enrollment: Enrollment, status: str, reason: str = "") -> None:
    enrollment.status = status
    enrollment.stopped_reason = reason
    enrollment.next_send_at = None


def _existing_outbound(db: Session, enrollment_id: str, step_id: str) -> Message | None:
    return db.scalar(
        select(Message).where(
            Message.enrollment_id == enrollment_id,
            Message.step_id == step_id,
            Message.direction == "outbound",
        )
    )


def send_step(
    db: Session,
    *,
    enrollment: Enrollment,
    sequence: Sequence,
    step: SequenceStep,
    candidate: Candidate,
    now: datetime,
    dry_run: bool = False,
) -> Message:
    settings = get_settings()
    existing = _existing_outbound(db, enrollment.id, step.id)
    if existing is not None and existing.status == "sent":
        return existing
    # Suppression is a permanent decision about a person, so a suppressed
    # failure stays sticky. A missing email is a temporary state of our data:
    # if it gets backfilled later, a re-run should retry rather than return the
    # stale failure forever.
    if existing is not None and existing.status == "failed" and existing.error == "suppressed":
        return existing

    to_address = normalize_email(candidate.primary_email or "")
    if to_address and is_suppressed(db, org_id=sequence.org_id, email=to_address):
        message = existing or Message(
            org_id=sequence.org_id,
            enrollment_id=enrollment.id,
            candidate_id=candidate.id,
            step_id=step.id,
            channel=step.channel,
            direction="outbound",
            to_address=to_address,
            status="failed",
            error="suppressed",
        )
        if existing is None:
            db.add(message)
            db.flush()
        else:
            message.status = "failed"
            message.error = "suppressed"
        _finish_enrollment(enrollment, "stopped", "suppressed")
        return message

    context = enrollment.personalization or None
    subject, body, context = build_message_content(
        db, sequence=sequence, step=step, candidate=candidate, context=context
    )
    if not enrollment.personalization:
        enrollment.personalization = context

    message = existing
    if message is None:
        message = Message(
            org_id=sequence.org_id,
            enrollment_id=enrollment.id,
            candidate_id=candidate.id,
            step_id=step.id,
            channel=step.channel,
            direction="outbound",
            to_address=to_address,
            subject=subject,
            body=body,
            status="queued",
        )
        try:
            with db.begin_nested():
                db.add(message)
                db.flush()
        except IntegrityError:
            message = _existing_outbound(db, enrollment.id, step.id)
            if message is None:
                raise
            if message.status == "sent":
                return message
    else:
        message.subject = subject
        message.body = body
        message.to_address = to_address

    if not to_address:
        message.status = "failed"
        message.error = "no email address on file"
        _finish_enrollment(enrollment, "stopped", "missing_email")
        return message

    sender = resolve_sender(db, sequence)
    token = signed_tracking_token(message.id)
    html_body = templates.add_tracking(
        templates.to_html(body),
        base_url=settings.base_url.rstrip("/"),
        message_id=message.id,
        token=token,
    )

    email = OutboundEmail(
        to=to_address,
        subject=subject,
        text_body=body,
        html_body=html_body,
        from_email=sequence.from_email or settings.outreach_from_email,
        from_name=sequence.from_name or settings.outreach_from_name,
        headers={"X-Juicebox-Message-Id": message.id},
    )

    if dry_run:
        message.status = "queued"
        message.provider_message_id = "dry-run"
        return message

    result = sender.send(email)
    if result.ok:
        message.status = "sent"
        message.sent_at = now
        message.provider_message_id = result.provider_message_id
        message.mailbox_id = sequence.mailbox_id or ""
        message.graph_message_id = result.graph_message_id
        message.internet_message_id = result.internet_message_id
        message.conversation_id = result.conversation_id
        message.error = ""
        record_event(
            db,
            org_id=sequence.org_id,
            type="outreach.sent",
            subject_type="message",
            subject_id=message.id,
            payload={"candidate_id": candidate.id, "step": step.step_order},
        )
    else:
        message.status = "failed"
        message.error = result.error
        record_event(
            db,
            org_id=sequence.org_id,
            type="outreach.failed",
            subject_type="message",
            subject_id=message.id,
            payload={"error": result.error},
        )
    return message


def due_enrollments(
    db: Session,
    *,
    org_id: str,
    now: datetime,
    sequence_id: str | None = None,
    limit: int | None = None,
) -> list[Enrollment]:
    query = (
        select(Enrollment)
        .join(Sequence, Sequence.id == Enrollment.sequence_id)
        .where(
            Sequence.org_id == org_id,
            Sequence.status == "active",
            Enrollment.status == "active",
            Enrollment.next_send_at.is_not(None),
            Enrollment.next_send_at <= now,
        )
        .options(selectinload(Enrollment.sequence).selectinload(Sequence.steps))
        .order_by(Enrollment.next_send_at)
    )
    if sequence_id:
        query = query.where(Enrollment.sequence_id == sequence_id)
    if limit:
        query = query.limit(limit)
    return list(db.scalars(query).all())


def process_enrollment(
    db: Session,
    enrollment: Enrollment,
    *,
    now: datetime,
    dry_run: bool = False,
    step_id: str | None = None,
) -> tuple[str, Message | None]:
    """Send the current step if it is due. Status is for the worker / run_due."""
    due_at = _aware(enrollment.next_send_at)
    if enrollment.status != "active":
        return "skipped", None

    sequence = enrollment.sequence
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    if enrollment.current_step >= len(steps):
        _finish_enrollment(enrollment, "completed")
        return "completed", None

    step = steps[enrollment.current_step]
    if step_id and step.id != step_id:
        # This send job is for a step that already ran.
        return "skipped", _existing_outbound(db, enrollment.id, step_id)

    if due_at is None or due_at > now:
        return "skipped", None

    if not dry_run:
        remaining_cap = max(
            0, get_settings().outreach_daily_cap - _sent_today(db, sequence.org_id, now)
        )
        if remaining_cap == 0:
            return "deferred", None

    candidate = db.get(Candidate, enrollment.candidate_id)
    if candidate is None:
        _finish_enrollment(enrollment, "stopped", "candidate_deleted")
        return "stopped", None

    step = steps[enrollment.current_step]
    message = send_step(
        db,
        enrollment=enrollment,
        sequence=sequence,
        step=step,
        candidate=candidate,
        now=now,
        dry_run=dry_run,
    )
    if message.status == "failed":
        return "failed", message
    if enrollment.status != "active":
        return "stopped", message

    enrollment.current_step += 1
    if enrollment.current_step >= len(steps):
        _finish_enrollment(enrollment, "completed")
        _advance_pipeline_stage(db, enrollment)
        return "completed", message
    next_step = steps[enrollment.current_step]
    enrollment.next_send_at = now + timedelta(days=max(next_step.delay_days, 0))
    _advance_pipeline_stage(db, enrollment)
    return "sent", message


def run_due(
    db: Session,
    *,
    org_id: str,
    now: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sequence_id: str | None = None,
) -> RunSummary:
    """Send every step that is due. Safe to call repeatedly (cron/worker)."""
    now = now or utcnow()
    summary = RunSummary()

    remaining_cap = max(0, get_settings().outreach_daily_cap - _sent_today(db, org_id, now))
    if remaining_cap == 0 and not dry_run:
        logger.info("daily outreach cap reached for org %s", org_id)
        return summary

    for enrollment in due_enrollments(
        db, org_id=org_id, now=now, sequence_id=sequence_id, limit=limit
    ):
        if not dry_run and summary.sent >= remaining_cap:
            summary.skipped += 1
            continue

        status, message = process_enrollment(db, enrollment, now=now, dry_run=dry_run)
        if status == "skipped":
            continue
        if status == "deferred":
            summary.skipped += 1
            continue
        if status == "completed" and message is None:
            summary.completed += 1
            continue
        if message is None:
            continue

        summary.processed += 1
        summary.messages.append(message)
        if message.status == "failed":
            summary.failed += 1
        else:
            summary.sent += 1
        if status == "completed" or enrollment.status == "completed":
            summary.completed += 1

    db.flush()
    return summary


def _advance_pipeline_stage(db: Session, enrollment: Enrollment) -> None:
    if not enrollment.project_id:
        return
    entry = db.scalar(
        select(ProjectCandidate).where(
            ProjectCandidate.project_id == enrollment.project_id,
            ProjectCandidate.candidate_id == enrollment.candidate_id,
        )
    )
    if entry is not None and entry.stage == "sourced":
        entry.stage = "contacted"


# --------------------------------------------------------------------------
# replies
# --------------------------------------------------------------------------


def record_reply(
    db: Session,
    *,
    message: Message,
    subject: str,
    body: str,
    now: datetime | None = None,
    mailbox_id: str = "",
    graph_message_id: str = "",
    internet_message_id: str = "",
    conversation_id: str = "",
) -> ReplyVerdict:
    """Attach an inbound reply to the outbound message and act on its intent.

    The graph ids are set when the reply arrived via mailbox sync, so the same
    inbound email is never recorded twice.
    """
    now = now or utcnow()
    verdict = classify_reply(subject, body)

    message.replied_at = message.replied_at or now
    message.reply_intent = verdict.intent
    message.reply_text = body

    inbound = Message(
        org_id=message.org_id,
        enrollment_id=message.enrollment_id,
        candidate_id=message.candidate_id,
        step_id=message.step_id,
        channel=message.channel,
        direction="inbound",
        to_address=message.to_address,
        subject=subject,
        body=body,
        status="received",
        reply_intent=verdict.intent,
        reply_text=body,
        mailbox_id=mailbox_id,
        graph_message_id=graph_message_id,
        internet_message_id=internet_message_id,
        conversation_id=conversation_id,
    )
    db.add(inbound)

    enrollment = db.get(Enrollment, message.enrollment_id)
    if enrollment is not None:
        if verdict.intent == "bounce":
            message.status = "bounced"
            _finish_enrollment(enrollment, "bounced", "bounced")
            suppress(
                db,
                org_id=message.org_id,
                email=message.to_address,
                reason="bounce",
                source="bounce",
                source_message_id=message.id,
            )
        elif verdict.intent == "unsubscribe":
            suppress(
                db,
                org_id=message.org_id,
                email=message.to_address,
                reason="unsubscribe",
                source="reply",
                source_message_id=message.id,
            )
            _finish_enrollment(enrollment, "replied", "unsubscribe")
        elif verdict.should_stop_sequence and enrollment.sequence.stop_on_reply:
            _finish_enrollment(enrollment, "replied", verdict.intent)
        if enrollment.project_id and verdict.intent not in {"bounce", "out_of_office"}:
            entry = db.scalar(
                select(ProjectCandidate).where(
                    ProjectCandidate.project_id == enrollment.project_id,
                    ProjectCandidate.candidate_id == enrollment.candidate_id,
                )
            )
            if entry is not None and entry.stage in {"sourced", "contacted"}:
                entry.stage = "replied"

    record_event(
        db,
        org_id=message.org_id,
        type="outreach.replied",
        subject_type="message",
        subject_id=message.id,
        payload={"intent": verdict.intent, "confidence": verdict.confidence},
    )
    db.flush()
    return verdict


def find_message_for_reply(
    db: Session, *, org_id: str, candidate_email: str | None, provider_message_id: str | None
) -> Message | None:
    """Match an inbound email back to the outbound message that provoked it."""
    if provider_message_id:
        message = db.scalar(
            select(Message).where(
                Message.org_id == org_id,
                Message.provider_message_id == provider_message_id,
            )
        )
        if message is not None:
            return message
    if candidate_email:
        return db.scalar(
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.to_address == candidate_email.lower(),
            )
            .order_by(Message.created_at.desc())
        )
    return None

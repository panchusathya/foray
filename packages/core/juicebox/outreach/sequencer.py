"""Multi-step outreach: enrolment, scheduling, sending, reply handling."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..llm.client import LLMUnavailable
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
from .ai_directives import resolve_directives
from .personalize import personalize, sender_context
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
# scheduling
# --------------------------------------------------------------------------


def _add_delay(base: datetime, days: int, unit: str) -> datetime:
    if unit == "business_days":
        result = base
        remaining = max(days, 0)
        while remaining > 0:
            result += timedelta(days=1)
            if result.weekday() < 5:
                remaining -= 1
        return result
    return base + timedelta(days=max(days, 0))


# How far after the FIRST email in a thread `suggested_date` lands. Three,
# because the spec is "two clear business days": send on Monday and the date is
# Thursday, leaving Tuesday and Wednesday free in between. Change this one
# number to shift the whole convention.
SUGGESTED_DATE_BUSINESS_DAYS = 3


def _outreach_anchor(db: Session, enrollment_id: str | None, now: datetime) -> datetime:
    """When outreach to this person actually started.

    Deliberately the first send on the enrollment, not this step's send time. A
    follow-up that recomputed from its own send date would propose a different
    day than the email before it, so the thread would contradict itself -- ask
    about Thursday on Monday, then about Tuesday on Thursday. Before the first
    send there is nothing to anchor to, so `now` is the start.
    """
    if not enrollment_id:
        return now
    first = db.scalar(
        select(Message.sent_at)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.direction == "outbound",
            Message.status == "sent",
            Message.sent_at.is_not(None),
        )
        .order_by(Message.sent_at.asc())
        .limit(1)
    )
    return _aware(first) or now


def suggested_date_context(
    db: Session,
    *,
    enrollment: Enrollment | None,
    step: SequenceStep | None,
    now: datetime,
) -> dict[str, str]:
    """A date to propose, stable across every step of one conversation.

    Rendered in the step's own timezone when it has one: the weekday is the
    whole point of the variable, and a UTC date flips a day either side of
    midnight for anyone outside Europe.
    """
    anchor = _outreach_anchor(db, getattr(enrollment, "id", None), now)
    zone = UTC
    name = getattr(step, "send_timezone", "") or ""
    if name:
        try:
            zone = ZoneInfo(name)
        except Exception:  # noqa: BLE001 - an unknown tz must not stop a send
            zone = UTC
    target = _add_delay(anchor.astimezone(zone), SUGGESTED_DATE_BUSINESS_DAYS, "business_days")
    # Built by hand rather than strftime: %-d is glibc-only and %#d is Windows.
    return {
        "suggested_date": f"{target.strftime('%A')} {target.day} {target.strftime('%B')}",
        "suggested_day": target.strftime("%A"),
    }


def schedule_step_at(step: SequenceStep, *, base: datetime) -> datetime:
    """When a step should send: delay (calendar or business days) from ``base``,
    snapped to the step's send_at_time in its timezone when one is set.

    The snap lands on the delay's end date even when that time of day is
    earlier than the delay boundary — "1 day later at 09:00" means tomorrow
    morning, not the day after. Only a time already in the past (a 0-delay
    step enrolled in the afternoon) rolls forward a day.
    """
    due = _add_delay(base, step.delay_days, step.delay_unit)
    if not step.send_at_time:
        return due
    try:
        zone = ZoneInfo(step.send_timezone) if step.send_timezone else UTC
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    hour, minute = (int(part) for part in step.send_at_time.split(":"))
    local = due.astimezone(zone).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if local < base.astimezone(zone):
        local += timedelta(days=1)
    if step.delay_unit == "business_days":
        while local.weekday() >= 5:
            local += timedelta(days=1)
    return local.astimezone(UTC)


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
    send_now: bool = False,
    now: datetime | None = None,
) -> list[Enrollment]:
    """Idempotent: re-enrolling an existing candidate returns the existing row.

    ``send_now`` overrides the first step's schedule for THIS enrolment only:
    a step pinned to 09:00 normally rolls a late-morning enrolment to tomorrow,
    which is correct for a batch but surprising when a recruiter adds one
    person and expects mail. Later steps keep their configured timing.
    """
    now = now or utcnow()
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    first_due = now if (send_now or not steps) else schedule_step_at(steps[0], base=now)

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
            next_send_at=first_due if start_immediately else None,
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
    enrollment: Enrollment | None = None,
    prior_fragments: dict[str, str] | None = None,
    connection=None,
    now: datetime | None = None,
) -> tuple[str, str, dict, dict[str, str]]:
    """Render one step. Raises ``LLMUnavailable`` when the step contains
    ``{{ai: ...}}`` directives and no model can resolve them — deliberately not
    caught here, so a directive's instruction text can never reach a candidate.

    ``prior_fragments`` (from a previous attempt's Message row) makes retries
    reuse the exact text that may already have gone out. The personalize()
    cache in ``enrollment.personalization`` is separate and never holds
    directive output: fragments are per-step, that cache is per-enrollment.
    """
    project = db.get(Project, sequence.project_id) if sequence.project_id else None
    if context is None:
        context = personalize(candidate, project, use_llm=sequence.personalize)

    # Merged on top of the personalize() result rather than baked into it: both
    # depend on the sending mailbox and the thread, not on the candidate, and
    # the personalize cache is keyed per candidate.
    context = {
        **context,
        **sender_context(
            connection, sequence, fallback_name=get_settings().outreach_from_name
        ),
        **suggested_date_context(
            db, enrollment=enrollment, step=step, now=now or utcnow()
        ),
    }

    fragments = resolve_directives(
        db,
        sequence=sequence,
        step=step,
        candidate=candidate,
        project=project,
        enrollment=enrollment,
        context=context,
        prior=prior_fragments,
    )

    subject_template = templates.render_directives(step.subject_template or "", fragments)
    body_template = templates.render_directives(step.body_template or "", fragments)
    subject = templates.render(subject_template, context).strip()
    body = templates.render(body_template, context).strip()
    if not subject:
        subject = context.get("subject_hint") or f"Quick note for {context.get('first_name', '')}".strip()
    return subject, body, context, fragments


def preview_step(
    db: Session,
    *,
    sequence: Sequence,
    candidate: Candidate,
    step_order: int = 0,
    enrollment: Enrollment | None = None,
) -> tuple[str, str, dict]:
    """Preview honours directives too; the router decides how to present an
    unresolvable one (banner + placeholder rather than a 500)."""
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    if not steps:
        raise ValueError("sequence has no steps")
    step = next((s for s in steps if s.step_order == step_order), steps[0])
    subject, body, context, _ = build_message_content(
        db, sequence=sequence, step=step, candidate=candidate, enrollment=enrollment
    )
    return subject, body, context


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


def _effective_daily_cap(db: Session, org_id: str) -> int:
    """The org-wide ceiling, lifted to whatever the mailbox pool can carry.

    OUTREACH_DAILY_CAP is a spend guard set for one sending identity. Left
    alone, five mailboxes capped at 40 each would sit under it and the pool
    would silently defer everything the moment it was fully used -- rotation
    that unlocks nothing. The pool's own per-mailbox caps are the real
    deliverability control.
    """
    from .rotation import pool_capacity

    return max(get_settings().outreach_daily_cap, pool_capacity(db, org_id=org_id))


def _thread_parent(db: Session, enrollment_id: str) -> Message | None:
    """The last message we actually got out on this enrollment.

    Its RFC Message-ID is what makes a follow-up land in the same thread in the
    candidate's client instead of arriving as an unrelated second email.
    """
    return db.scalar(
        select(Message)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.direction == "outbound",
            Message.status == "sent",
            Message.internet_message_id != "",
        )
        .order_by(Message.sent_at.desc())
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
    resolved=None,
) -> Message:
    """Send one step. ``resolved`` is the mailbox process_enrollment already
    picked; passing it avoids resolving (and re-counting caps) twice."""
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

    # Resolved before rendering, not after: the signature is now a variable
    # that depends on which mailbox is sending, so the body cannot be built
    # until that is known.
    resolved = resolved or resolve_sender(db, sequence, enrollment, now=now)

    context = enrollment.personalization or None
    try:
        subject, body, context, fragments = build_message_content(
            db,
            sequence=sequence,
            step=step,
            candidate=candidate,
            context=context,
            enrollment=enrollment,
            prior_fragments=(existing.ai_fragments or None) if existing else None,
            connection=getattr(resolved, "connection", None),
            now=now,
        )
    except LLMUnavailable as exc:
        # Fail closed: a step with {{ai: ...}} directives must never send the
        # raw instruction text. The enrollment stays active so the step retries
        # once the model is back; the delay stops run_due hot-looping on it.
        message = existing or Message(
            org_id=sequence.org_id,
            enrollment_id=enrollment.id,
            candidate_id=candidate.id,
            step_id=step.id,
            channel=step.channel,
            direction="outbound",
            to_address=to_address,
            status="failed",
            error=f"ai_directive_unresolved: {exc}"[:490],
        )
        if existing is None:
            db.add(message)
            db.flush()
        else:
            message.status = "failed"
            message.error = f"ai_directive_unresolved: {exc}"[:490]
        enrollment.next_send_at = now + timedelta(minutes=15)
        return message
    if not enrollment.personalization:
        enrollment.personalization = context

    plain_body = templates.to_text(body)
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
            body=plain_body,
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
        message.body = plain_body
        message.to_address = to_address
    if fragments:
        message.ai_fragments = fragments

    if not to_address:
        message.status = "failed"
        message.error = "no email address on file"
        _finish_enrollment(enrollment, "stopped", "missing_email")
        return message

    sender = resolved.sender
    token = signed_tracking_token(message.id)
    html_body = templates.add_tracking(
        templates.to_html(body),
        base_url=settings.base_url.rstrip("/"),
        message_id=message.id,
        token=token,
    )

    # Follow-ups thread off the previous send. Providers that cannot express
    # these headers ignore them and send a fresh message rather than failing.
    parent = _thread_parent(db, enrollment.id)
    email = OutboundEmail(
        to=to_address,
        subject=subject,
        text_body=plain_body,
        html_body=html_body,
        from_email=sequence.from_email or settings.outreach_from_email,
        from_name=sequence.from_name or settings.outreach_from_name,
        headers={"X-Juicebox-Message-Id": message.id},
        in_reply_to=parent.internet_message_id if parent is not None else "",
        references=parent.internet_message_id if parent is not None else "",
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
        # The mailbox that actually sent it, not the sequence's preference --
        # under rotation those differ, and the cap query reads this column.
        connection = getattr(resolved, "connection", None)
        message.mailbox_id = connection.id if connection is not None else ""
        message.provider_native_id = result.provider_native_id
        message.internet_message_id = result.internet_message_id
        message.conversation_id = result.conversation_id
        message.error = ""
        if connection is not None and not enrollment.mailbox_id:
            # Pin on first success: every later step of this conversation goes
            # out from the same address.
            enrollment.mailbox_id = connection.id
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
    # Sessions are autoflush=False, so without this the row this send just
    # produced is invisible to the next enrollment's cap query in the same pass
    # -- every daily cap would be overshot by the size of the batch.
    db.flush()
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

    resolved = None
    if not dry_run:
        remaining_cap = max(
            0, _effective_daily_cap(db, sequence.org_id) - _sent_today(db, sequence.org_id, now)
        )
        if remaining_cap == 0:
            return "deferred", None
        # Resolving here rather than inside send_step keeps a full mailbox from
        # touching the enrollment at all: "deferred" leaves next_send_at alone
        # so the worker requeues this same permanent idempotency key. Raising
        # would park the job and the step could never send again; advancing
        # next_send_at would make the requeue return "skipped", which marks the
        # job succeeded and loses the step just as permanently.
        resolved = resolve_sender(db, sequence, enrollment, now=now)
        if resolved.deferred:
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
        resolved=resolved,
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
    enrollment.next_send_at = schedule_step_at(next_step, base=now)
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

    remaining_cap = max(0, _effective_daily_cap(db, org_id) - _sent_today(db, org_id, now))
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
    provider_native_id: str = "",
    internet_message_id: str = "",
    conversation_id: str = "",
) -> ReplyVerdict:
    """Attach an inbound reply to the outbound message and act on its intent.

    The provider ids are set when the reply arrived via mailbox sync, so the
    same inbound email is never recorded twice.
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
        # The reply came FROM the candidate's address; sent_at makes the
        # timeline sort real instead of falling back to created_at.
        from_address=message.to_address,
        to_address=message.to_address,
        subject=subject,
        body=body,
        status="received",
        sent_at=now,
        reply_intent=verdict.intent,
        reply_text=body,
        mailbox_id=mailbox_id,
        provider_native_id=provider_native_id,
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

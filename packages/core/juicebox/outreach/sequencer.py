"""Multi-step outreach: enrolment, scheduling, sending, reply handling."""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..crm import track_all
from ..jobs import KIND_OUTREACH_SEND
from ..llm.client import LLMUnavailable
from ..models import (
    Candidate,
    Enrollment,
    Job,
    Message,
    Org,
    Project,
    ProjectCandidate,
    Sequence,
    SequenceStep,
    new_id,
    record_event,
    utcnow,
)
from ..security import signed_intake_token, signed_tracking_token
from . import templates
from .ai_directives import resolve_directives
from .personalize import personalize, sender_context
from .replies import HUMAN_INTENTS, ReplyVerdict, _stop_for, classify_reply
from .sender import OutboundEmail, preview_connection, resolve_sender
from .suppressions import is_suppressed, normalize_email, suppress

logger = logging.getLogger(__name__)

# How long a held outreach.send job sleeps before the worker asks again.
# The cap-defer is ~60s (a midnight reset is worth polling); a sequence
# paused for a month is not.
HOLD_DEFER = timedelta(minutes=5)


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


def schedule_step_at(step: SequenceStep, *, base: datetime, ignore_delay: bool = False) -> datetime:
    """When a step should send: delay (calendar or business days) from ``base``,
    snapped to the step's send_at_time in its timezone when one is set.

    The snap lands on the delay's end date even when that time of day is
    earlier than the delay boundary — "1 day later at 09:00" means tomorrow
    morning, not the day after. Only a time already in the past (a 0-delay
    step enrolled in the afternoon) rolls forward a day.

    ``ignore_delay`` keeps the time-of-day snap and the weekend roll but
    counts no delay: the caller has already decided the day (an out-of-office
    return date) and only wants it landed at the step's usual hour.
    """
    due = base if ignore_delay else _add_delay(base, step.delay_days, step.delay_unit)
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
    skip_recent_repliers: bool = True,
) -> list[Enrollment]:
    """Idempotent: re-enrolling an existing candidate returns the existing row.

    ``send_now`` overrides the first step's schedule for THIS enrolment only:
    a step pinned to 09:00 normally rolls a late-morning enrolment to tomorrow,
    which is correct for a batch but surprising when a recruiter adds one
    person and expects mail. Later steps keep their configured timing.

    ``skip_recent_repliers`` drops anyone who answered us by hand within the
    quiet period (``recent_human_replies``): a person mid-conversation on one
    sequence was enrolled in a re-created copy three hours after replying and
    got two more automated steps. Callers that have already applied (or
    deliberately overridden) the guard pass False.
    """
    now = now or utcnow()
    # One row per person even when the payload names them twice. Rows created
    # in the loop below are not in the `existing` map, so a repeated id used to
    # insert a second Enrollment and trip the (sequence_id, candidate_id)
    # unique constraint -- which rolls back everybody else in the request, not
    # just the duplicate.
    candidate_ids = list(dict.fromkeys(cid for cid in candidate_ids if cid))
    if skip_recent_repliers and candidate_ids:
        blocked = recent_human_replies(
            db,
            org_id=sequence.org_id,
            candidate_ids=candidate_ids,
            now=now,
            days=get_settings().outreach_reply_quiet_days,
        )
        candidate_ids = [cid for cid in candidate_ids if cid not in blocked]
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    first_due = now if (send_now or not steps) else schedule_step_at(steps[0], base=now)

    # Choosing to email someone is a CRM action: it is what makes their replies
    # worth keeping, and without it a contact could drop out of the CRM the
    # moment they leave their last project.
    track_all(db, org_id=sequence.org_id, candidate_ids=candidate_ids)

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


def _needs_written_variables(step: SequenceStep) -> bool:
    """Whether rendering this step actually consumes model-written text.

    personalize() costs a live model call -- 10s+ on a reasoning model -- and is
    gated only on sequence.personalize, never on whether the template refers to
    anything it produces. Four of six steps in production referenced none of
    them, so every preview and every send paid for output that was discarded.

    An empty subject template counts as a use: build_message_content falls back
    to context["subject_hint"] to title the email, so the value is consumed even
    though no {{ }} names it.

    AI directives deliberately do NOT count. They make their own model call, and
    the deterministic profile fields are all their prompt needs as context -- so
    a step with directives and no written variables gets one call, not two.
    """
    if not (step.subject_template or "").strip():
        return True
    return templates.uses_llm_variables(
        f"{step.subject_template or ''}\n{step.body_template or ''}"
    )


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
    strict: bool = True,
) -> tuple[str, str, dict, dict[str, str]]:
    """Render one step. Raises ``LLMUnavailable`` when the step contains
    ``{{ai: ...}}`` directives and no model can resolve them — deliberately not
    caught here, so a directive's instruction text can never reach a candidate.

    Raises ``MalformedTemplate`` for the same reason, and first: render() can
    only substitute what its patterns match, so a placeholder they do not claim
    survives as literal braces in the sent body. Checked before personalize()
    rather than after, so a template that cannot send costs no model call.

    ``strict=False`` skips only that check, for preview — see preview_step.

    ``prior_fragments`` (from a previous attempt's Message row) makes retries
    reuse the exact text that may already have gone out. The personalize()
    cache in ``enrollment.personalization`` is separate and never holds
    directive output: fragments are per-step, that cache is per-enrollment.
    """
    if strict:
        broken = templates.malformed_placeholders(
            step.subject_template or ""
        ) + templates.malformed_placeholders(step.body_template or "")
        if broken:
            raise templates.MalformedTemplate(", ".join(dict.fromkeys(broken)))
    project = db.get(Project, sequence.project_id) if sequence.project_id else None
    if context is None:
        # sequence.personalize stays the operator's off switch; this only ever
        # skips work whose result nothing reads.
        use_llm = sequence.personalize and _needs_written_variables(step)
        context = personalize(candidate, project, use_llm=use_llm)

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
        subject = (
            context.get("subject_hint") or f"Quick note for {context.get('first_name', '')}".strip()
        )
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
    unresolvable one (banner + placeholder rather than a 500).

    ``strict=False`` below is deliberate: a preview must show what would
    actually go out, and for a malformed placeholder that is the raw braces.
    Raising here would replace the one screen that makes the mistake obvious
    with an error. The router pairs the rendered body with a named warning;
    sending still refuses."""
    steps = sorted(sequence.steps, key=lambda s: s.step_order)
    if not steps:
        raise ValueError("sequence has no steps")
    step = next((s for s in steps if s.step_order == step_order), steps[0])
    # Render as the mailbox that would actually send this, or the signature in
    # the preview is not the signature the candidate gets -- which is the whole
    # point of previewing.
    connection = preview_connection(db, sequence, enrollment)
    subject, body, context, _ = build_message_content(
        db,
        sequence=sequence,
        step=step,
        candidate=candidate,
        enrollment=enrollment,
        connection=connection,
        strict=False,
    )
    if step is not steps[0]:
        # Follow-ups inherit the first step's line -- show the recruiter the
        # subject the candidate will actually see, not one this step's own
        # (now hidden, possibly stale) template would have produced.
        #
        # A live enrollment has the real thing on its first sent message. With
        # no enrollment there is nothing sent yet, so the first step's template
        # is rendered instead, with any {{ai:}} directive left as a visible
        # marker: resolving one here would mean a second model round-trip for a
        # subject the send path never renders at all.
        root = _root_subject(db, enrollment.id) if enrollment is not None else ""
        if not root:
            root = templates.render(
                templates.mark_directives(steps[0].subject_template or ""), context
            ).strip()
        if root:
            subject = reply_subject(root)
    return subject, body, context


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------


def send_spacing() -> timedelta:
    """Shortest gap allowed between two emails from one org. Zero disables it."""
    return timedelta(seconds=max(get_settings().outreach_send_spacing_seconds, 0))


def send_gap(*parts: object) -> timedelta:
    """The gap in front of one particular send: somewhere in [min, max].

    A fixed sixty seconds is itself a machine signature, so each send gets its
    own gap. It is a hash of what is being sent rather than ``random``, so a
    tick and the Outbox that previews it deal the same minute.
    """
    settings = get_settings()
    low = max(settings.outreach_send_spacing_seconds, 0)
    high = max(settings.outreach_send_spacing_max_seconds, low)
    if high == low:
        return timedelta(seconds=low)
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()
    return timedelta(seconds=low + int(digest[:8], 16) % (high - low + 1))


def last_mail_sent_at(db: Session, org_id: str) -> datetime | None:
    """The org's most recent automated email, from any path.

    ``orgs.last_mail_sent_at`` is written by every mailbox send we make
    (``note_mail_sent``); the message table is read as well because sequence
    sends made before that column existed only live there.
    """
    org = db.get(Org, org_id)
    stamped = _aware(org.last_mail_sent_at) if org is not None else None
    sequenced = _aware(
        db.scalar(
            select(func.max(Message.sent_at)).where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.status == "sent",
                Message.enrollment_id.is_not(None),
            )
        )
    )
    return max((v for v in (stamped, sequenced) if v is not None), default=None)


def note_mail_sent(db: Session, org_id: str, at: datetime) -> None:
    """Start the gap clock. Call it after every successful mailbox send."""
    org = db.get(Org, org_id)
    if org is None:
        return
    at = _aware(at)
    current = _aware(org.last_mail_sent_at)
    if current is None or at > current:
        org.last_mail_sent_at = at


def spacing_wait(db: Session, org_id: str, now: datetime) -> datetime | None:
    """When this org may send its next email, if that is later than now.

    Enrolling fifty people at "09:00" used to hand the worker fifty send jobs
    for the same instant, and one mailbox fired all fifty inside a second --
    the burst pattern spam filters look for. This is the hard gate every send
    path goes through (the worker, "Run now", enrol-and-send-now). The gap after
    each send is its own, somewhere in [min, max], keyed on that send's time.
    """
    if not send_spacing():
        return None
    last = last_mail_sent_at(db, org_id)
    if last is None:
        return None
    opens = last + send_gap(org_id, int(last.timestamp()))
    return opens if opens > now else None


# -- the line ---------------------------------------------------------------
#
# Every due outreach.send job carries a rank (Job.rank, lower goes first).
# The outreach tick is the dispatcher: each minute it wakes the best-ranked
# few that are not waiting on a full mailbox and leaves the rest asleep.
# Pre-dealing every job its own future minute was the first design, and it
# stalled: a slot that came up for a follow-up pinned to a full mailbox sent
# nothing, while first emails that any free mailbox could carry sat hours
# down the line. Ranks survive any number of retries; run_at does not.

WAKE_WINDOW = 5
SLEEP = timedelta(minutes=10)
BLOCKED_KEY = "blocked_until"


def _blocked_until(job: Job) -> datetime | None:
    raw = (job.payload or {}).get(BLOCKED_KEY)
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        return None


def open_send_jobs(db: Session, org_id: str) -> list[Job]:
    """The org's queued send jobs in line order: rank, then age."""
    rows = db.scalars(
        select(Job).where(
            Job.org_id == org_id,
            Job.kind == KIND_OUTREACH_SEND,
            Job.status == "queued",
        )
    ).all()
    return sorted(
        rows,
        key=lambda j: (
            j.rank is None,
            j.rank if j.rank is not None else 0.0,
            _aware(j.created_at) or datetime.min.replace(tzinfo=UTC),
        ),
    )


def next_rank(db: Session, org_id: str) -> float:
    """One past the back of the line."""
    top = db.scalar(
        select(func.max(Job.rank)).where(
            Job.org_id == org_id,
            Job.kind == KIND_OUTREACH_SEND,
            Job.status.in_(("queued", "running")),
        )
    )
    return float(top or 0) + 1.0


def dispatch_line(db: Session, org_id: str, now: datetime) -> dict[str, int]:
    """Wake the head of the line; let everyone else sleep.

    A job blocked on a full mailbox sleeps until that mailbox has room and is
    skipped here until then, so one stuck follow-up never holds the gap.
    """
    wake_at = spacing_wait(db, org_id, now) or now
    awake = asleep = blocked = 0
    for job in open_send_jobs(db, org_id):
        until = _blocked_until(job)
        if until is not None and until > now:
            blocked += 1
            if _aware(job.run_at) < until:
                job.run_at = until
            continue
        if awake < WAKE_WINDOW:
            awake += 1
            if _aware(job.run_at) > wake_at:
                job.run_at = wake_at
            continue
        asleep += 1
        # Only push a sleeper further out; never pull it in, which would
        # rewrite every row of a long line every minute.
        if _aware(job.run_at) < now + SLEEP / 2:
            job.run_at = now + SLEEP
    return {"awake": awake, "asleep": asleep, "blocked": blocked}


def mark_blocked(job: Job, until: datetime | None, now: datetime) -> datetime:
    """Record that this send waits on a full mailbox; returns its next run."""
    until = _aware(until) or (now + HOLD_DEFER)
    job.payload = {**(job.payload or {}), BLOCKED_KEY: until.isoformat()}
    return until


def clear_blocked(job: Job) -> None:
    if (job.payload or {}).get(BLOCKED_KEY):
        job.payload = {k: v for k, v in job.payload.items() if k != BLOCKED_KEY}


MOVES = ("top", "up", "down", "bottom", "position")


def move_in_line(
    db: Session,
    org_id: str,
    *,
    enrollment_ids: list[str],
    to: str,
    position: int | None = None,
    now: datetime,
) -> int:
    """Move the named rows within the line; returns how many moved.

    Acts on the whole org's line, not on one page of it, and keeps the moved
    rows in their existing relative order -- selecting #4 and #9 and pressing
    "top" makes them #1 and #2, never #2 and #1. Ids not in line (already
    sent, or not yet queued) are ignored rather than refused: the Outbox may
    be a few seconds stale by the time the button is pressed.
    """
    if to not in MOVES:
        raise ValueError(f"unknown move {to!r}")
    line = open_send_jobs(db, org_id)
    wanted = set(enrollment_ids)
    picked = [j for j in line if (j.payload or {}).get("enrollment_id") in wanted]
    if not picked:
        return 0
    ids = {j.id for j in picked}
    rest = [j for j in line if j.id not in ids]

    if to == "top":
        order = picked + rest
    elif to == "bottom":
        order = rest + picked
    elif to == "position":
        index = max(0, min((position or 1) - 1, len(rest)))
        order = rest[:index] + picked + rest[index:]
    else:
        # One step, as a block: each picked row swaps with the nearest
        # unpicked neighbour on that side; rows already at the edge stay.
        order = list(line)
        indexes = range(len(order)) if to == "up" else range(len(order) - 1, -1, -1)
        step = -1 if to == "up" else 1
        for i in indexes:
            j = i + step
            if order[i].id in ids and 0 <= j < len(order) and order[j].id not in ids:
                order[i], order[j] = order[j], order[i]

    for index, job in enumerate(order, start=1):
        job.rank = float(index)
    dispatch_line(db, org_id, now)
    return len(picked)


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


def _reference_chain(db: Session, enrollment_id: str) -> str:
    """Every Message-ID on this conversation so far, oldest first.

    References is a chain, not a pointer: a client that never saw the message
    In-Reply-To names walks back through the rest of it to find the thread. Two
    steps in, a References holding only the immediate parent is exactly the
    case where that walk fails, and the follow-up opens as its own
    conversation.

    Inbound messages count -- a candidate's reply is part of the thread we are
    continuing.
    """
    ids = db.scalars(
        select(Message.internet_message_id)
        .where(
            Message.enrollment_id == enrollment_id,
            Message.internet_message_id != "",
            Message.sent_at.is_not(None),
        )
        .order_by(Message.sent_at.asc())
    ).all()
    seen: list[str] = []
    for value in ids:
        if value not in seen:
            seen.append(value)
    return " ".join(seen)


def _root_subject(db: Session, enrollment_id: str) -> str:
    """The subject the first email on this enrollment actually went out under.

    Read off the sent Message rather than re-rendered from a template: it is
    the literal line sitting in the candidate's client, so a template edited
    between steps cannot move a live conversation. Deliberately not restricted
    to messages carrying an internetMessageId the way ``_thread_parent`` is --
    subject continuity is worth having on providers that give us no RFC id.
    """
    return (
        db.scalar(
            select(Message.subject)
            .where(
                Message.enrollment_id == enrollment_id,
                Message.direction == "outbound",
                Message.status == "sent",
            )
            .order_by(Message.sent_at.asc())
        )
        or ""
    ).strip()


def reply_subject(root: str) -> str:
    """``root`` as a reply subject, without stacking a second "Re:"."""
    root = (root or "").strip()
    if not root:
        return ""
    return root if root.lower().startswith("re:") else f"Re: {root}"


def _thread_subject(subject: str, root: str) -> str:
    """The subject a follow-up goes out under.

    Every step after the first inherits the first one's line, full stop. Gmail
    and Outlook both split a conversation on a changed subject, so a follow-up
    free to render its own was free to break the thread no matter how correct
    the RFC headers were -- and the recruiter who typed two slightly different
    lines into two steps had no way to know that is what they had done. The
    step editor no longer offers the choice; this is the same rule on the send
    side, where it also covers sequences authored before that.

    Falls back to the step's own subject only when nothing has been sent on the
    enrollment yet, which is the first step and any provider-level retry of it.
    """
    root = (root or "").strip()
    return reply_subject(root) if root else subject


def _finish_enrollment(enrollment: Enrollment, status: str, reason: str = "") -> None:
    enrollment.status = status
    enrollment.stopped_reason = reason
    enrollment.next_send_at = None


def _failed_message(
    db: Session,
    *,
    existing: Message | None,
    sequence: Sequence,
    enrollment: Enrollment,
    candidate: Candidate,
    step: SequenceStep,
    to_address: str,
    error: str,
) -> Message:
    """Record a send failure on this step's row, reusing a prior attempt's.

    Every caller wants the same thing -- one row per (enrollment, step),
    marked failed with a reason short enough for the column -- and each was
    writing its own copy of it.
    """
    error = error[:490]
    if existing is not None:
        existing.status = "failed"
        existing.error = error
        return existing
    message = Message(
        org_id=sequence.org_id,
        enrollment_id=enrollment.id,
        candidate_id=candidate.id,
        step_id=step.id,
        channel=step.channel,
        direction="outbound",
        to_address=to_address,
        status="failed",
        error=error,
    )
    db.add(message)
    db.flush()
    return message


def _existing_outbound(db: Session, enrollment_id: str, step_id: str) -> Message | None:
    return db.scalar(
        select(Message).where(
            Message.enrollment_id == enrollment_id,
            Message.step_id == step_id,
            Message.direction == "outbound",
        )
    )


def _reach_score_for(address: str, scores: dict) -> int | None:
    if address in scores:
        try:
            return int(scores[address])
        except (TypeError, ValueError):
            return None
    for key, value in scores.items():
        if isinstance(key, str) and key.lower() == address:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _email_reach_scores(candidate: Candidate) -> dict:
    """Every address score we hold, whichever importer wrote it.

    Scores used to be read from ``raw.foray.email_reach`` alone, because Foray
    was the only thing that produced them. The CSV importer scores addresses
    too -- it has Juicebox's own Deliverable/Risky/Undeliverable verdict -- and
    writing those under ``foray`` to be seen would be a lie about where they
    came from, so the lookup spans namespaces instead.

    ``foray`` is merged last so it still wins on a conflict: its scores are
    measured, and this function must not change what an existing Foray-sourced
    candidate resolves to.
    """
    payload = candidate.raw if isinstance(candidate.raw, dict) else {}
    merged: dict = {}
    for namespace in sorted(payload):
        if namespace == "foray":
            continue
        section = payload.get(namespace)
        if not isinstance(section, dict):
            continue
        scores = section.get("email_reach")
        if isinstance(scores, dict):
            merged.update(scores)
    foray = payload.get("foray")
    if isinstance(foray, dict) and isinstance(foray.get("email_reach"), dict):
        merged.update(foray["email_reach"])
    return merged


def _sendable_address(candidate: Candidate, floor: int) -> tuple[str, str | None]:
    """First address that clears the reach-score floor, preserving list order.

    The floor skips; it does not re-rank. Unscored addresses (no ``email_reach``
    entry under any namespace in ``raw``) pass, because the gate only applies
    when we actually scored them. Returns ``(address, error)``; ``error`` is
    ``reach_score_below_floor`` when every scored address is below ``floor``.
    """
    emails = [normalize_email(raw or "") for raw in (candidate.emails or [])]
    emails = [addr for addr in emails if addr]
    if not emails:
        return "", None
    if floor <= 0:
        return emails[0], None
    scores = _email_reach_scores(candidate)
    saw_score = False
    for addr in emails:
        score = _reach_score_for(addr, scores)
        if score is None:
            return addr, None
        saw_score = True
        if score >= floor:
            return addr, None
    if saw_score:
        return "", "reach_score_below_floor"
    return emails[0], None


def _preview_message(
    *,
    sequence: Sequence,
    enrollment: Enrollment,
    candidate: Candidate,
    step: SequenceStep,
    **fields,
) -> Message:
    """The row a dry run *would* have written, deliberately never persisted.

    A preview still has to hand a Message back -- the API serialises it and the
    CLI counts it -- but must not leave one behind. The request that asked for
    the preview is committed by ``CommittingRoute``, so a row added "just to
    return it" is permanent, and a persisted step makes ``process_enrollment``
    advance an enrollment nobody actually mailed.

    Column defaults only fire on flush, so the ones the API reads are filled in
    by hand; without that the response serialises nulls into non-null fields.
    """
    stamp = utcnow()
    defaults = {
        "id": new_id("msg"),
        "source": "sequence",
        "from_address": "",
        "to_address": "",
        "subject": "",
        "body": "",
        "status": "queued",
        "error": "",
        # Marks the row as a preview wherever one is inspected, the same way
        # the persisted dry-run rows used to.
        "provider_message_id": "dry-run",
        "mailbox_id": "",
        "provider_native_id": "",
        "internet_message_id": "",
        "conversation_id": "",
        "open_count": 0,
        "click_count": 0,
        "reply_intent": "",
        "reply_text": "",
        "ai_fragments": {},
        "body_html": "",
        "web_link": "",
        "attachment_count": 0,
        "created_at": stamp,
        "updated_at": stamp,
    }
    return Message(
        org_id=sequence.org_id,
        enrollment_id=enrollment.id,
        candidate_id=candidate.id,
        step_id=step.id,
        channel=step.channel,
        direction="outbound",
        **{**defaults, **fields},
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
    picked; passing it avoids resolving (and re-counting caps) twice.

    ``dry_run`` renders the step and writes nothing at all -- see
    ``_preview_message``.
    """
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

    to_address, reach_error = _sendable_address(candidate, settings.min_reach_score)
    if to_address and is_suppressed(db, org_id=sequence.org_id, email=to_address):
        if dry_run:
            return _preview_message(
                sequence=sequence,
                enrollment=enrollment,
                candidate=candidate,
                step=step,
                to_address=to_address,
                status="failed",
                error="suppressed",
            )
        message = _failed_message(
            db,
            existing=existing,
            sequence=sequence,
            enrollment=enrollment,
            candidate=candidate,
            step=step,
            to_address=to_address,
            error="suppressed",
        )
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
        if dry_run:
            return _preview_message(
                sequence=sequence,
                enrollment=enrollment,
                candidate=candidate,
                step=step,
                to_address=to_address,
                status="failed",
                error=f"ai_directive_unresolved: {exc}"[:490],
            )
        message = _failed_message(
            db,
            existing=existing,
            sequence=sequence,
            enrollment=enrollment,
            candidate=candidate,
            step=step,
            to_address=to_address,
            error=f"ai_directive_unresolved: {exc}",
        )
        enrollment.next_send_at = now + timedelta(minutes=15)
        return message
    except templates.MalformedTemplate as exc:
        # Same fail-closed reason -- a placeholder the renderer cannot parse
        # reaches the candidate as raw braces -- but a different wait. An
        # unavailable model comes back on its own; a typo'd placeholder needs a
        # person to edit the step, so retrying in 15 minutes just re-fails.
        # An hour is long enough for that edit and short enough that it then
        # sends on its own, without anyone re-enrolling the candidate.
        logger.warning(
            "step %s has unparseable placeholders (%s); not sending", step.id, exc
        )
        if dry_run:
            return _preview_message(
                sequence=sequence,
                enrollment=enrollment,
                candidate=candidate,
                step=step,
                to_address=to_address,
                status="failed",
                error=f"malformed_template: {exc}"[:490],
            )
        message = _failed_message(
            db,
            existing=existing,
            sequence=sequence,
            enrollment=enrollment,
            candidate=candidate,
            step=step,
            to_address=to_address,
            error=f"malformed_template: {exc}",
        )
        enrollment.next_send_at = now + timedelta(hours=1)
        return message
    # Not on a dry run: the cache is written for a send that is not happening,
    # and it would pin the copy a later real send renders.
    if not dry_run and not enrollment.personalization:
        enrollment.personalization = context

    plain_body = templates.to_text(body)
    # Before the Message row: the parent decides the subject, and the row has
    # to record what actually went out.
    parent = _thread_parent(db, enrollment.id)
    subject = _thread_subject(subject, _root_subject(db, enrollment.id))

    if dry_run:
        # Everything below this line writes: the Message row, the enrollment's
        # stop reason, and the send itself. A preview stops here with the copy
        # it rendered and nothing else.
        return _preview_message(
            sequence=sequence,
            enrollment=enrollment,
            candidate=candidate,
            step=step,
            to_address=to_address,
            subject=subject,
            body=plain_body,
            from_address=sequence.from_email or settings.outreach_from_email,
            status="queued" if to_address else "failed",
            error="" if to_address else (reach_error or "no email address on file"),
            ai_fragments=fragments or {},
        )

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
        if reach_error == "reach_score_below_floor":
            message.error = "reach_score_below_floor"
            _finish_enrollment(enrollment, "stopped", "reach_score_below_floor")
        else:
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
        sign_click=lambda target: signed_intake_token(f"click:{message.id}", target),
        opens=sequence.track_opens,
        clicks=sequence.track_clicks,
    )

    # Follow-ups thread off the previous send. Providers that cannot express
    # these headers ignore them and send a fresh message rather than failing.
    #
    # thread_native_id only when the parent left from the mailbox that is
    # sending now: replying on a thread means asking that mailbox for one of
    # its own messages, and another mailbox's id is not a message it has.
    # Pinning normally guarantees this, but a re-pin or a fixed-mode change can
    # move an in-flight enrollment.
    connection = getattr(resolved, "connection", None)
    same_mailbox = (
        parent is not None
        and connection is not None
        and parent.mailbox_id == connection.id
        and bool(parent.provider_native_id)
    )
    email = OutboundEmail(
        to=to_address,
        subject=subject,
        text_body=plain_body,
        html_body=html_body,
        from_email=sequence.from_email or settings.outreach_from_email,
        from_name=sequence.from_name or settings.outreach_from_name,
        headers={"X-Juicebox-Message-Id": message.id},
        in_reply_to=parent.internet_message_id if parent is not None else "",
        references=_reference_chain(db, enrollment.id) if parent is not None else "",
        thread_native_id=parent.provider_native_id if same_mailbox else "",
    )
    # Who we sent as, so an inbound recorded against this row is not
    # candidate-to-candidate. Set even on a failed attempt: it is the
    # address we tried.
    message.from_address = email.from_email

    result = sender.send(email)
    if result.ok:
        message.status = "sent"
        message.sent_at = now
        note_mail_sent(db, sequence.org_id, now)
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


def sending_hold(
    db: Session, sequence: Sequence, *, now: datetime, org: Org | None = None
) -> str | None:
    """Why outbound is held for this sequence right now, or None.

    Org kill switch first, then sequence pause. The daily cap is a different
    deferral — it is checked later in ``process_enrollment`` so the outbox can
    name the reason separately.
    """
    del now  # holds are not time-windowed; the signature matches every other gate
    if org is None:
        org = db.get(Org, sequence.org_id)
    if org is not None and org.outreach_paused_at is not None:
        return "org_paused"
    if sequence.status != "active":
        return "sequence_paused"
    return None


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
        .join(Org, Org.id == Sequence.org_id)
        .where(
            Sequence.org_id == org_id,
            Sequence.status == "active",
            Org.outreach_paused_at.is_(None),
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
    if sending_hold(db, sequence, now=now):
        # Deferred, not skipped: skipped would mark the permanent send key
        # succeeded and the step would be lost. The worker requeues this
        # same key, so unpausing releases the backlog at cap rate.
        return "deferred", None

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

    # Somebody who answered us by hand -- on this thread, another sequence's,
    # or a manual email -- is in a conversation, and automated mail on top of
    # it reads as exactly what it is. This is the hard gate; record_reply
    # stops what it can see, but a forwarded copy, a second enrolment made
    # after the reply, or a stop_on_reply=False sibling all slipped past it.
    hit = recent_human_replies(
        db,
        org_id=sequence.org_id,
        candidate_ids=[enrollment.candidate_id],
        now=now,
        days=get_settings().outreach_reply_quiet_days,
    ).get(enrollment.candidate_id)
    if hit is not None and not (
        # The operator switched this sequence's own reply-stop off; honour
        # that for replies on this very enrolment, and only those.
        hit.enrollment_id == enrollment.id and not sequence.stop_on_reply
    ):
        if dry_run:
            return "skipped", None
        _finish_enrollment(enrollment, "replied", "replied_elsewhere")
        enrollment.replied_at = _aware(hit.sent_at) or now
        record_event(
            db,
            org_id=sequence.org_id,
            type="outreach.stopped",
            subject_type="enrollment",
            subject_id=enrollment.id,
            payload={
                "reason": "replied_elsewhere",
                "candidate_id": enrollment.candidate_id,
                "source_message_id": hit.id,
                "reply_intent": hit.reply_intent,
            },
        )
        return "stopped", None

    resolved = None
    if not dry_run:
        remaining_cap = max(
            0, _effective_daily_cap(db, sequence.org_id) - _sent_today(db, sequence.org_id, now)
        )
        if remaining_cap == 0:
            return "deferred", None
        if spacing_wait(db, sequence.org_id, now) is not None:
            # Too soon after the last send. Deferred for the same reason a
            # full cap is: the permanent key must survive to send later.
            return "deferred", None
        # Resolving here rather than inside send_step keeps a full mailbox from
        # touching the enrollment at all: "deferred" leaves next_send_at alone
        # so the worker requeues this same permanent idempotency key. Raising
        # would park the job and the step could never send again; advancing
        # next_send_at would make the requeue return "skipped", which marks the
        # job succeeded and loses the step just as permanently.
        resolved = resolve_sender(db, sequence, enrollment, now=now)
        if resolved.deferred:
            # Not mapped; read by the worker to sleep the job until the
            # blocking mailbox has room instead of retrying every minute.
            enrollment.mailbox_retry_at = resolved.retry_at or now + HOLD_DEFER
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

    if dry_run:
        # Nothing was sent, so nothing may move. Advancing here skipped the
        # step for real: CommittingRoute commits the preview request, so a
        # dry run over 50 enrollments left all 50 on step 2, marked contacted,
        # having received nothing.
        return "sent", message

    enrollment.current_step += 1
    enrollment.snoozed_until = None
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


def recent_human_replies(
    db: Session,
    *,
    org_id: str,
    candidate_ids: list[str],
    now: datetime,
    days: int,
) -> dict[str, Message]:
    """The newest hand-written reply per candidate within the quiet period.

    One query for any number of people. Only classified human intents count:
    an auto-responder is not a conversation, and thread-synced mail with no
    intent (an unmatched note to a recruiter's own inbox) would otherwise
    block a person for a month on the strength of nothing.
    """
    if days <= 0 or not candidate_ids:
        return {}
    rows = db.scalars(
        select(Message)
        .where(
            Message.org_id == org_id,
            Message.candidate_id.in_(candidate_ids),
            Message.direction == "inbound",
            Message.reply_intent.in_(sorted(HUMAN_INTENTS)),
            Message.sent_at >= now - timedelta(days=days),
        )
        .order_by(Message.sent_at.desc())
    ).all()
    newest: dict[str, Message] = {}
    for row in rows:
        newest.setdefault(row.candidate_id, row)
    return newest


_FINGERPRINT_WS = re.compile(r"\s+")


def _reply_fingerprint(text: str) -> str:
    collapsed = _FINGERPRINT_WS.sub(" ", (text or "").lower()).strip()
    if not collapsed:
        return ""
    return hashlib.sha1(collapsed[:2000].encode("utf-8", errors="replace")).hexdigest()


def _duplicate_inbound(
    db: Session, *, org_id: str, candidate_id: str, body: str, since: datetime
) -> Message | None:
    """An inbound row from this person with the same words, recorded recently.

    Persona mailboxes auto-forward to a central inbox, so one reply arrives
    twice with two native ids, two thread ids and (on Zoho) no RFC id at all --
    nothing the idempotency index can see. The words are the same, though.
    """
    fingerprint = _reply_fingerprint(body)
    if not fingerprint:
        return None
    rows = db.scalars(
        select(Message).where(
            Message.org_id == org_id,
            Message.candidate_id == candidate_id,
            Message.direction == "inbound",
            Message.sent_at >= since,
        )
    ).all()
    for row in rows:
        if _reply_fingerprint(row.reply_text or row.body) == fingerprint:
            return row
    return None


def _active_enrollments_for(
    db: Session, *, org_id: str, candidate_id: str, except_id: str | None
) -> list[Enrollment]:
    return list(
        db.scalars(
            select(Enrollment)
            .join(Sequence, Sequence.id == Enrollment.sequence_id)
            .where(
                Sequence.org_id == org_id,
                Enrollment.candidate_id == candidate_id,
                Enrollment.status == "active",
                Enrollment.id != (except_id or ""),
            )
            .options(selectinload(Enrollment.sequence).selectinload(Sequence.steps))
        ).all()
    )


def _stop_sibling_enrollments(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    except_id: str | None,
    reason: str,
    now: datetime,
    source_message_id: str | None,
    respect_stop_on_reply: bool = True,
) -> int:
    """A person who answered one sequence is done with all of them.

    Brett replied "interested" to one campaign and was mailed twice more by
    its re-created copy: the copy's enrollment never heard about the reply.
    Each sibling's own stop_on_reply is honoured unless the reply is an
    opt-out, which no sequence setting gets to override.
    """
    stopped = 0
    for enrollment in _active_enrollments_for(
        db, org_id=org_id, candidate_id=candidate_id, except_id=except_id
    ):
        if respect_stop_on_reply and not enrollment.sequence.stop_on_reply:
            continue
        _finish_enrollment(enrollment, "replied", reason)
        enrollment.replied_at = now
        record_event(
            db,
            org_id=org_id,
            type="outreach.stopped",
            subject_type="enrollment",
            subject_id=enrollment.id,
            payload={
                "reason": reason,
                "candidate_id": candidate_id,
                "source_message_id": source_message_id,
            },
        )
        stopped += 1
    return stopped


def _snooze_enrollment(
    enrollment: Enrollment, *, follow_up_after: date | None, now: datetime
) -> None:
    """Push the next step past an auto-responder's return date.

    The date named is when they are back; the step lands the day after, at
    the step's usual hour. With no date, a small default push keeps the
    follow-up from landing on top of the auto-reply. Never moves a send
    earlier, and never touches an enrollment that is not waiting to send.
    """
    if enrollment.status != "active" or enrollment.next_send_at is None:
        return
    if follow_up_after is not None:
        target = datetime.combine(follow_up_after + timedelta(days=1), time.min, tzinfo=UTC)
    else:
        days = get_settings().outreach_ooo_default_snooze_days
        if days <= 0:
            return
        target = now + timedelta(days=days)
    current = _aware(enrollment.next_send_at)
    if current is not None and target <= current:
        return
    steps = sorted(enrollment.sequence.steps, key=lambda s: s.step_order)
    step = steps[enrollment.current_step] if enrollment.current_step < len(steps) else None
    due = schedule_step_at(step, base=target, ignore_delay=True) if step else target
    enrollment.next_send_at = due
    enrollment.snoozed_until = due


def follow_up_reminder_at(
    enrollment: Enrollment, follow_up_after: date, *, now: datetime
) -> datetime:
    """08:00 the day after the date they named, in the step's timezone."""
    steps = sorted(enrollment.sequence.steps, key=lambda s: s.step_order)
    zone = UTC
    if steps:
        step = steps[min(enrollment.current_step, len(steps) - 1)]
        if step.send_timezone:
            try:
                zone = ZoneInfo(step.send_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                zone = UTC
    local = datetime.combine(follow_up_after + timedelta(days=1), time(8, 0), tzinfo=zone)
    run_at = local.astimezone(UTC)
    return run_at if run_at > now else now


def _schedule_follow_up_reminder(
    db: Session, *, enrollment: Enrollment, inbound: Message, now: datetime
) -> None:
    from ..jobs import KIND_OUTREACH_FOLLOW_UP, enqueue

    if enrollment.follow_up_after is None:
        return
    enqueue(
        db,
        org_id=enrollment.sequence.org_id,
        kind=KIND_OUTREACH_FOLLOW_UP,
        # A later reply naming a different date makes a different job; the
        # handler checks the enrollment's current date and lets the stale
        # one no-op.
        idempotency_key=(
            f"{KIND_OUTREACH_FOLLOW_UP}:{enrollment.id}:{enrollment.follow_up_after.isoformat()}"
        ),
        payload={
            "org_id": enrollment.sequence.org_id,
            "enrollment_id": enrollment.id,
            "inbound_message_id": inbound.id,
            "follow_up_after": enrollment.follow_up_after.isoformat(),
        },
        run_at=follow_up_reminder_at(enrollment, enrollment.follow_up_after, now=now),
    )


def record_reply(
    db: Session,
    *,
    message: Message,
    subject: str,
    body: str,
    now: datetime | None = None,
    received_at: datetime | None = None,
    mailbox_id: str = "",
    provider_native_id: str = "",
    internet_message_id: str = "",
    conversation_id: str = "",
) -> ReplyVerdict:
    """Attach an inbound reply to the outbound message and act on its intent.

    The provider ids are set when the reply arrived via mailbox sync, so the
    same inbound email is never recorded twice. ``received_at`` is when the
    mail actually arrived; the quiet-period guards and the follow-up date
    both count from it, not from when the sync happened to run.
    """
    now = now or utcnow()
    received_at = _aware(received_at) or now

    duplicate = _duplicate_inbound(
        db,
        org_id=message.org_id,
        candidate_id=message.candidate_id,
        body=body,
        since=received_at - timedelta(days=3),
    )
    if duplicate is not None:
        intent = duplicate.reply_intent or "interested"
        return ReplyVerdict(
            intent=intent,
            confidence=1.0,
            should_stop_sequence=_stop_for(intent),
            summary="Duplicate of an already-recorded reply",
            classified_by="duplicate",
        )

    verdict = classify_reply(subject, body, received_at=received_at)

    message.replied_at = message.replied_at or received_at
    message.reply_intent = verdict.intent
    message.reply_text = body

    inbound = Message(
        org_id=message.org_id,
        enrollment_id=message.enrollment_id,
        candidate_id=message.candidate_id,
        step_id=message.step_id,
        channel=message.channel,
        direction="inbound",
        # The reply came FROM the candidate's address, TO the mailbox we
        # sent from. Writing both as `to_address` made every inbound look
        # like the candidate mailing themselves.
        from_address=message.to_address,
        to_address=message.from_address,
        subject=subject,
        body=body,
        status="received",
        sent_at=received_at,
        reply_intent=verdict.intent,
        reply_text=body,
        mailbox_id=mailbox_id,
        provider_native_id=provider_native_id,
        internet_message_id=internet_message_id,
        conversation_id=conversation_id,
    )
    db.add(inbound)
    db.flush()

    # A bounce and an opt-out are facts about a person and a message, not about
    # a sequence, so they are recorded whether or not one is behind the mail.
    # Thread-synced outbound carries no enrollment by design -- a recruiter
    # mailing someone by hand from Outlook -- and while these lived inside the
    # enrollment branch, "please take me off your list" on such a thread wrote
    # no Suppression at all, so enrolling that person later mailed them again.
    if verdict.intent == "bounce":
        message.status = "bounced"
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

    enrollment = db.get(Enrollment, message.enrollment_id) if message.enrollment_id else None
    except_id = enrollment.id if enrollment is not None else None
    stopped_elsewhere = 0
    if verdict.intent == "bounce":
        if enrollment is not None:
            _finish_enrollment(enrollment, "bounced", "bounced")
    elif verdict.intent == "unsubscribe":
        if enrollment is not None:
            _finish_enrollment(enrollment, "replied", "unsubscribe")
            enrollment.replied_at = enrollment.replied_at or received_at
        stopped_elsewhere = _stop_sibling_enrollments(
            db,
            org_id=message.org_id,
            candidate_id=message.candidate_id,
            except_id=except_id,
            reason="unsubscribe",
            now=received_at,
            source_message_id=inbound.id,
            respect_stop_on_reply=False,
        )
    elif verdict.intent == "out_of_office":
        # A machine answered. The person is away until the date it names, so
        # every sequence waiting on them waits until they are back.
        targets = list(
            _active_enrollments_for(
                db, org_id=message.org_id, candidate_id=message.candidate_id, except_id=except_id
            )
        )
        if enrollment is not None:
            targets.insert(0, enrollment)
        for target in targets:
            _snooze_enrollment(target, follow_up_after=verdict.follow_up_after, now=now)
    else:
        # A person wrote back. Whatever they said, automated mail is over for
        # them -- on this sequence if it stops on reply, and on every other
        # active one regardless of which thread they happened to answer.
        if enrollment is not None:
            if enrollment.sequence.stop_on_reply:
                _finish_enrollment(enrollment, "replied", verdict.intent)
                enrollment.replied_at = enrollment.replied_at or received_at
            if verdict.intent == "not_now" and verdict.follow_up_after is not None:
                enrollment.follow_up_after = verdict.follow_up_after
                _schedule_follow_up_reminder(db, enrollment=enrollment, inbound=inbound, now=now)
        stopped_elsewhere = _stop_sibling_enrollments(
            db,
            org_id=message.org_id,
            candidate_id=message.candidate_id,
            except_id=except_id,
            reason="replied_elsewhere",
            now=received_at,
            source_message_id=inbound.id,
        )

    if enrollment is not None and enrollment.project_id and verdict.intent in HUMAN_INTENTS:
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
        payload={
            "intent": verdict.intent,
            "confidence": verdict.confidence,
            "classified_by": verdict.classified_by,
            "follow_up_after": (
                verdict.follow_up_after.isoformat() if verdict.follow_up_after else ""
            ),
            "stopped_elsewhere": stopped_elsewhere,
            # The subject is a message, which has no page. Every other
            # outreach event names the person here; this one did not, so a
            # reply was the one activity row nobody could click through.
            "candidate_id": message.candidate_id,
        },
    )
    db.flush()
    return verdict


def find_message_for_reply(
    db: Session,
    *,
    org_id: str,
    candidate_email: str | None,
    provider_message_id: str | None,
    mailbox_id: str | None = None,
) -> Message | None:
    """Match an inbound email back to the outbound message that provoked it.

    ``provider_message_id`` is overloaded on purpose: mailbox sync passes the
    provider's native id, and an ESP inbound-parse webhook passes RFC
    ``In-Reply-To``. Both are stored on the outbound row (native id vs
    ``internet_message_id``), so looking at only one silently dropped the
    other.

    ``mailbox_id`` is the mailbox the reply landed in. The address fallback
    prefers an outbound sent FROM that mailbox: a person enrolled in two
    sequences, sent from two mailboxes, replies to one -- and the org-wide
    "newest mail to this address" guess stamped the other sequence's row.
    (Not ``from_address``: every outbound carries the sequence's from_email,
    whichever mailbox actually sent it.)
    """
    if provider_message_id:
        message = db.scalar(
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                or_(
                    Message.provider_message_id == provider_message_id,
                    Message.provider_native_id == provider_message_id,
                    Message.internet_message_id == provider_message_id,
                ),
            )
            .order_by(Message.created_at.desc())
        )
        if message is not None:
            return message
    if candidate_email:
        by_address = (
            select(Message)
            .where(
                Message.org_id == org_id,
                Message.direction == "outbound",
                Message.to_address == candidate_email.lower(),
            )
            .order_by(Message.created_at.desc())
        )
        if mailbox_id:
            message = db.scalar(by_address.where(Message.mailbox_id == mailbox_id))
            if message is not None:
                return message
        return db.scalar(by_address)
    return None

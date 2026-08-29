"""Funnel, activity and to-do reporting.

Three read-only views of the same workspace, in widening order of opinion:
``/funnel`` counts, ``/activity`` narrates, and ``/todo`` says what is stuck.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..models import (
    PIPELINE_STAGES,
    Application,
    Enrollment,
    Event,
    IntakeCall,
    IntakeThread,
    Message,
    Project,
    ProjectCandidate,
    Sequence,
    utcnow,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/analytics", tags=["analytics"])

POSITIVE_INTENTS = {"interested", "question", "referral"}


@router.get("/funnel")
def funnel(
    project_id: str | None = None,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    stage_stmt = (
        select(ProjectCandidate.stage, func.count(ProjectCandidate.id))
        .join(Project, Project.id == ProjectCandidate.project_id)
        .where(Project.org_id == principal.org_id)
        .group_by(ProjectCandidate.stage)
    )
    if project_id:
        stage_stmt = stage_stmt.where(ProjectCandidate.project_id == project_id)

    stages = {stage: 0 for stage in PIPELINE_STAGES}
    for stage, count in db.execute(stage_stmt):
        stages[stage] = int(count)

    message_stmt = select(Message).where(
        Message.org_id == principal.org_id, Message.direction == "outbound"
    )
    if project_id:
        # Attribute a send to the project its *sequence* belongs to, so these
        # counts describe exactly the sequences the page lists. Going through
        # Enrollment.project_id instead would read a snapshot taken at enroll
        # time, which drifts the moment a sequence is reassigned. The inner
        # join also drops mailbox-synced mail (enrollment_id is NULL), which
        # no sequence -- and therefore no project -- is responsible for.
        message_stmt = (
            message_stmt.join(Enrollment, Enrollment.id == Message.enrollment_id)
            .join(Sequence, Sequence.id == Enrollment.sequence_id)
            .where(Sequence.project_id == project_id)
        )
    messages = db.scalars(message_stmt).all()

    sent = sum(1 for m in messages if m.status == "sent")
    opened = sum(1 for m in messages if m.opened_at is not None)
    clicked = sum(1 for m in messages if m.clicked_at is not None)
    replied = sum(1 for m in messages if m.replied_at is not None)
    bounced = sum(1 for m in messages if m.status == "bounced")
    positive = sum(1 for m in messages if m.reply_intent in POSITIVE_INTENTS)

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    return {
        "project_id": project_id,
        "stages": stages,
        "outreach": {
            "sent": sent,
            "opened": opened,
            "clicked": clicked,
            "replied": replied,
            "bounced": bounced,
            "positive_replies": positive,
        },
        "reply_rate": rate(replied, sent),
        "open_rate": rate(opened, sent),
        "click_rate": rate(clicked, sent),
        "positive_reply_rate": rate(positive, sent),
    }




@router.get("/activity")
def activity(
    limit: int = 50,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.scalars(
        select(Event)
        .where(Event.org_id == principal.org_id)
        .order_by(Event.created_at.desc())
        .limit(min(limit, 200))
    ).all()

    # Every outreach event names the *message*, which has no page of its own.
    # The person does, so resolve it here in one query rather than leaving the
    # UI with a row it cannot open. `outreach.replied` is the reason this
    # exists: its payload never carried a candidate_id.
    message_ids = {
        row.subject_id
        for row in rows
        if row.subject_type == "message"
        and row.subject_id
        and not (row.payload or {}).get("candidate_id")
    }
    resolved: dict[str, str] = {}
    if message_ids:
        resolved = dict(
            db.execute(
                select(Message.id, Message.candidate_id).where(
                    Message.org_id == principal.org_id, Message.id.in_(message_ids)
                )
            ).all()
        )

    out = []
    for row in rows:
        payload = row.payload or {}
        candidate_id = payload.get("candidate_id") or ""
        if not candidate_id and row.subject_type == "candidate":
            candidate_id = row.subject_id
        if not candidate_id and row.subject_type == "message":
            candidate_id = resolved.get(row.subject_id, "")
        out.append(
            {
                "id": row.id,
                "type": row.type,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "candidate_id": candidate_id,
                "payload": payload,
                "created_at": row.created_at,
            }
        )
    return out


# The grace a reply gets before it counts as owed an answer. Something that
# landed an hour ago is not a backlog item; something from Tuesday is.
REPLY_SLA_HOURS = 24


@router.get("/todo")
def todo(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> dict:
    """What is actually waiting on a human, most urgent first.

    Deliberately not "everything that could be done": every row is something
    that stalls until a person acts, and every count is its own query rather
    than a stage total the funnel already reports. An empty list is a real
    answer, and the UI says so rather than inventing filler.

    Copy and destinations live in the web app (``src/lib/todo.js``); this
    returns keys, counts and ages, so rewording a line is not an API deploy.
    """
    org_id = principal.org_id
    now = utcnow()
    items: list[dict] = []

    # Replies nobody has answered. The comparison is per candidate: the last
    # thing they sent us against the last thing we sent them. An inbound with
    # nothing after it is a person waiting on us.
    inbound_latest: dict[str, datetime] = {}
    outbound_latest: dict[str, datetime] = {}
    for candidate_id, direction, stamp in db.execute(
        select(
            Message.candidate_id,
            Message.direction,
            func.max(func.coalesce(Message.sent_at, Message.created_at)),
        )
        .where(Message.org_id == org_id)
        .group_by(Message.candidate_id, Message.direction)
    ):
        if stamp is None:
            continue
        bucket = inbound_latest if direction == "inbound" else outbound_latest
        bucket[candidate_id] = stamp

    owed = [
        last_in
        for candidate_id, last_in in inbound_latest.items()
        if not (
            (last_out := outbound_latest.get(candidate_id)) is not None and last_out >= last_in
        )
        and _hours_since(now, last_in) >= REPLY_SLA_HOURS
    ]
    if owed:
        items.append(
            {
                "key": "replies_waiting",
                "count": len(owed),
                "oldest_hours": round(_hours_since(now, min(owed)), 1),
                "tone": "urgent",
            }
        )

    # Applications the machine has parked on a human.
    app_counts = {
        row[0]: row[1]
        for row in db.execute(
            select(Application.status, func.count())
            .where(Application.org_id == org_id)
            .group_by(Application.status)
        )
    }
    if app_counts.get("awaiting_consent"):
        items.append(
            {
                "key": "applications_awaiting_consent",
                "count": app_counts["awaiting_consent"],
                "tone": "waiting",
            }
        )
    if app_counts.get("prepared"):
        items.append(
            {"key": "applications_prepared", "count": app_counts["prepared"], "tone": "ready"}
        )

    failed_packages = db.scalar(
        select(func.count())
        .select_from(Application)
        .where(Application.org_id == org_id, Application.package_error != "")
    )
    if failed_packages:
        items.append({"key": "packages_failed", "count": failed_packages, "tone": "urgent"})

    # Intake a recruiter still has to read or attribute. Calls and texts are
    # the same chore, so they are one line.
    intake_review = (
        db.scalar(
            select(func.count())
            .select_from(IntakeCall)
            .where(IntakeCall.org_id == org_id, IntakeCall.review_needed.is_(True))
        )
        or 0
    ) + (
        db.scalar(
            select(func.count())
            .select_from(IntakeThread)
            .where(IntakeThread.org_id == org_id, IntakeThread.review_needed.is_(True))
        )
        or 0
    )
    if intake_review:
        items.append({"key": "intake_review", "count": intake_review, "tone": "waiting"})

    # Sends that failed outright. Invisible until somebody looks.
    undelivered = db.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.org_id == org_id,
            Message.direction == "outbound",
            Message.status.in_(("failed", "bounced")),
        )
    )
    if undelivered:
        items.append({"key": "sends_failed", "count": undelivered, "tone": "urgent"})

    return {"items": items, "generated_at": now}


def _hours_since(now: datetime, then: datetime) -> float:
    """Tolerant of a naive stamp: SQLite hands datetimes back without a zone."""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, (now - then).total_seconds() / 3600.0)

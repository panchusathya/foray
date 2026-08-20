"""Funnel and activity reporting."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..models import PIPELINE_STAGES, Event, Message, Project, ProjectCandidate

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
    return [
        {
            "id": row.id,
            "type": row.type,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "payload": row.payload,
            "created_at": row.created_at,
        }
        for row in rows
    ]

"""Rematch queue: declined contacts with better-fitting open jobs."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import (
    Candidate,
    Enrollment,
    JobMatch,
    JobPosting,
    Message,
    Project,
    Suppression,
    utcnow,
)
from ..outreach.suppressions import normalize_email


def rematch_queue(db: Session, org_id: str, *, now=None, limit: int = 50) -> list[dict]:
    now = now or utcnow()
    settings = get_settings()
    latest = (
        select(Message.candidate_id, func.max(Message.sent_at).label("sent_at"))
        .where(
            Message.org_id == org_id,
            Message.direction == "inbound",
            Message.reply_intent != "",
        )
        .group_by(Message.candidate_id)
        .subquery()
    )
    rows = db.execute(
        select(Message, Candidate)
        .join(
            latest,
            (Message.candidate_id == latest.c.candidate_id)
            & (Message.sent_at == latest.c.sent_at),
        )
        .join(Candidate, Candidate.id == Message.candidate_id)
        .where(
            Message.org_id == org_id,
            Message.direction == "inbound",
            Message.reply_intent.in_(("not_interested", "not_now")),
        )
    ).all()

    suppressed = {
        normalize_email(e)
        for e in db.scalars(select(Suppression.email).where(Suppression.org_id == org_id)).all()
        if e
    }
    active = {
        row
        for row in db.scalars(
            select(Enrollment.candidate_id).where(
                Enrollment.status == "active",
                Enrollment.candidate_id.in_([c.id for _, c in rows] or ["__none__"]),
            )
        ).all()
    }

    gate = timedelta(days=max(settings.job_rematch_not_now_days, 1))
    out: list[dict] = []
    for message, candidate in rows:
        if any(normalize_email(str(e)) in suppressed for e in (candidate.emails or [])):
            continue
        if candidate.id in active:
            continue
        if message.reply_intent == "not_now":
            sent = message.sent_at or message.created_at
            if sent is not None and now - sent < gate:
                continue
        declined_project_id, declined_label, declined_on = _declined_role(db, message)
        fits = _better_fits(
            db,
            org_id=org_id,
            candidate_id=candidate.id,
            declined_project_id=declined_project_id,
            limit=5,
        )
        if not fits:
            continue
        out.append(
            {
                "candidate": candidate,
                "reply_intent": message.reply_intent,
                "declined_project_id": declined_project_id,
                "declined_label": declined_label,
                "declined_on": declined_on,
                "matches": fits,
            }
        )
        if len(out) >= limit:
            break
    return out


def _declined_role(db: Session, message: Message) -> tuple[str | None, str, object]:
    enrollment = None
    if message.enrollment_id:
        enrollment = db.get(Enrollment, message.enrollment_id)
    project = None
    if enrollment and enrollment.project_id:
        project = db.get(Project, enrollment.project_id)
    label = ""
    if project is not None:
        role = project.role_title or project.name
        company = project.company or ""
        label = f"{role} at {company}" if company else role
    when = message.sent_at or message.created_at
    return (project.id if project else None), label, when


def _better_fits(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    declined_project_id: str | None,
    limit: int,
) -> list[JobMatch]:
    settings = get_settings()
    stmt = (
        select(JobMatch)
        .join(JobPosting, JobPosting.id == JobMatch.job_posting_id)
        .where(
            JobMatch.org_id == org_id,
            JobMatch.candidate_id == candidate_id,
            JobMatch.status != "dismissed",
            JobMatch.score >= settings.job_match_min_score,
            JobPosting.status == "open",
        )
        .order_by(JobMatch.score.desc())
        .limit(limit + 5)
    )
    rows = list(db.scalars(stmt).all())
    kept: list[JobMatch] = []
    posting_ids = [row.job_posting_id for row in rows]
    postings = {
        p.id: p
        for p in db.scalars(select(JobPosting).where(JobPosting.id.in_(posting_ids or ["__none__"]))).all()
    }
    for row in rows:
        posting = postings.get(row.job_posting_id)
        if posting is None:
            continue
        if declined_project_id and posting.project_id == declined_project_id:
            continue
        kept.append(row)
        if len(kept) >= limit:
            break
    return kept

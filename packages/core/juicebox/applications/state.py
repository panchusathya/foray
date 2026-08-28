"""The application status machine.

Using JuiceBox authorizes applications on the candidate's behalf; a separate
per-application approval is not required.  Transitions and submission limits
remain centralized here so a caller cannot bypass them accidentally.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    APPLICATION_STATUSES,
    APPLICATION_TERMINAL_STATUSES,
    PIPELINE_STAGES,
    Application,
    ProjectCandidate,
    record_event,
    utcnow,
)
from . import limits

logger = logging.getLogger(__name__)

TRANSITIONS: dict[str, set[str]] = {
    "draft": {"prepared", "withdrawn"},
    # prepared -> draft is a rebuild request; prepared -> submitted is the
    # thread case where the yes arrived before the package ("apply to 2").
    "prepared": {"awaiting_consent", "submitting", "submitted", "draft", "withdrawn"},
    "awaiting_consent": {"submitting", "submitted", "prepared", "withdrawn"},
    # An agent attempt is in flight. It ends at `submitted`, or back at
    # `prepared` with a status_reason -- the package still exists and the
    # recorded consent is untouched, so `prepared -> submitting` is the retry.
    "submitting": {"submitted", "prepared", "withdrawn"},
    "submitted": {"acknowledged", "interviewing", "rejected", "withdrawn"},
    "acknowledged": {"interviewing", "rejected", "withdrawn"},
    "interviewing": {"offer", "rejected", "withdrawn"},
    # An accepted offer lives on ProjectCandidate as "hired"; the application
    # record stops at offer or its two honest ends.
    "offer": {"rejected", "withdrawn"},
    "rejected": set(),
    "withdrawn": set(),
}

# Application status -> the recruiter pipeline stage it implies, when the
# application belongs to a project. One-way and forward-only: a stage a
# recruiter moved further along is never pulled back, and outreach-driven
# stages (contacted/replied) are never touched from here.
_STAGE_FOR_STATUS = {
    "submitted": "screening",
    "acknowledged": "screening",
    "interviewing": "interviewing",
    "offer": "offer",
    "rejected": "rejected",
}


class TransitionError(ValueError):
    """An illegal move. Routers translate this to a 409."""


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, set())


def transition(
    db: Session,
    application: Application,
    *,
    to_status: str,
    actor: str,
    actor_id: str | None = None,
    reason: str = "",
    occurred_at: datetime | None = None,
    now: datetime | None = None,
) -> Application:
    """Move one application one step, with every guard in one place."""
    now = now or utcnow()
    if to_status not in APPLICATION_STATUSES:
        raise TransitionError(f"unknown application status {to_status!r}")
    if not can_transition(application.status, to_status):
        raise TransitionError(
            f"cannot move an application from {application.status!r} to {to_status!r}"
        )

    # The submission volume limit applies both on the way into an attempt and
    # on the way out of one.  This keeps direct status changes from bypassing
    # the same limit enforced by request_submission.
    if to_status in {"submitting", "submitted"}:
        decision = limits.may_submit(db, application=application, now=now)
        if not decision:
            raise TransitionError(f"submission budget: {decision.detail}")

    previous = application.status
    application.status = to_status
    if reason:
        application.status_reason = reason[:240]

    if to_status == "prepared":
        application.prepared_at = now
    elif to_status == "submitting":
        application.submitting_at = now
    elif to_status == "submitted":
        application.submitted_at = now
        application.submitted_by = actor
        if actor_id:
            application.submitted_by_user_id = actor_id
    elif to_status == "acknowledged":
        application.acknowledged_at = now
    if to_status in APPLICATION_TERMINAL_STATUSES:
        application.closed_at = now

    _sync_project_stage(db, application)

    record_event(
        db,
        org_id=application.org_id,
        type="application.status_changed",
        subject_type="application",
        subject_id=application.id,
        actor_id=actor_id,
        payload={
            "from": previous,
            "to": to_status,
            "actor": actor,
            "reason": reason,
            # Only recorded when it differs from "now" -- a stamp equal to
            # created_at adds nothing, and the history renders the event's own
            # clock when this is absent.
            **(
                {"occurred_at": occurred_at.isoformat()}
                if occurred_at is not None
                else {}
            ),
        },
    )

    # A recruiter moving the pursuit is news to the candidate; a candidate's
    # own action is not -- nobody wants a text announcing what they just did.
    if actor == "recruiter":
        from ..messaging.notify import EVENT_STATUS_CHANGED, notify_candidate

        notify_candidate(
            db,
            org_id=application.org_id,
            candidate_id=application.candidate_id,
            event=EVENT_STATUS_CHANGED,
            subject_id=f"{application.id}:{to_status}",
        )

    db.flush()
    return application


def _sync_project_stage(db: Session, application: Application) -> None:
    """Forward-only echo into the recruiter pipeline, when there is one."""
    if not application.project_id:
        return
    stage = _STAGE_FOR_STATUS.get(application.status)
    if stage is None:
        return
    row = db.scalar(
        select(ProjectCandidate).where(
            ProjectCandidate.project_id == application.project_id,
            ProjectCandidate.candidate_id == application.candidate_id,
        )
    )
    if row is None:
        return
    try:
        current_rank = PIPELINE_STAGES.index(row.stage)
        target_rank = PIPELINE_STAGES.index(stage)
    except ValueError:
        return
    if target_rank > current_rank:
        row.stage = stage

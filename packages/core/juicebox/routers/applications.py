"""Recruiter-side application lifecycle API.

The candidate-side surface (portal, thread) reaches the same
``applications.service`` functions through its own principal; this router is
the recruiter's window — list, inspect, transition, rebuild packages. A
``TransitionError`` is a 409: the machine refused, nothing broke.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    create_application,
    current_documents,
    request_package,
)
from ..applications.state import TransitionError, transition
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import APPLICATION_STATUSES, Application, Candidate, Event, JobPosting
from ..schemas import (
    ApplicationCreate,
    ApplicationDetailOut,
    ApplicationHistoryEntry,
    ApplicationOut,
    ApplicationPatch,
    ApplicationTransitionIn,
    CandidateDocumentOut,
)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/applications", tags=["applications"])


def _load(db: Session, application_id: str, org_id: str) -> Application:
    application = db.scalar(
        select(Application).where(
            Application.id == application_id, Application.org_id == org_id
        )
    )
    if application is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Application not found"
        )
    return application


def _history(db: Session, application: Application) -> list[ApplicationHistoryEntry]:
    """Replay this application's recorded moves, oldest first.

    Every transition already writes ``application.status_changed``; reading it
    back is cheaper and truer than a parallel table, which could disagree with
    the row it describes.
    """
    rows = db.scalars(
        select(Event)
        .where(
            Event.org_id == application.org_id,
            Event.subject_type == "application",
            Event.subject_id == application.id,
            Event.type == "application.status_changed",
        )
        .order_by(Event.created_at.asc())
    ).all()
    entries = []
    for row in rows:
        payload = row.payload or {}
        stated = payload.get("occurred_at")
        entries.append(
            ApplicationHistoryEntry(
                at=row.created_at,
                occurred_at=_parse_stamp(stated),
                from_status=str(payload.get("from") or ""),
                to_status=str(payload.get("to") or ""),
                actor=str(payload.get("actor") or ""),
                note=str(payload.get("reason") or ""),
            )
        )
    return entries


def _parse_stamp(value: object) -> datetime | None:
    """A hand-entered date that no longer parses is dropped, never fatal."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _detail(
    db: Session, application: Application, *, with_history: bool = False
) -> ApplicationDetailOut:
    # History is off by default: the list route calls this per row, and one
    # event query per application is a page of N+1 nobody reads.
    posting = db.get(JobPosting, application.job_posting_id)
    candidate = db.get(Candidate, application.candidate_id)
    documents = list(current_documents(db, application).values())
    out = ApplicationDetailOut.model_validate(application)
    out.company = posting.company if posting else ""
    out.title = posting.title if posting else ""
    out.posting_url = posting.source_url if posting else ""
    out.candidate_name = candidate.full_name if candidate else ""
    out.documents = [CandidateDocumentOut.model_validate(doc) for doc in documents]
    if with_history:
        out.history = _history(db, application)
    return out


@router.post("", response_model=ApplicationOut, status_code=201)
def create(
    payload: ApplicationCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> ApplicationOut:
    try:
        application = create_application(
            db,
            org_id=principal.org_id,
            candidate_id=payload.candidate_id,
            job_posting_id=payload.job_posting_id,
            source=payload.source,
            created_by=principal.user_id,
        )
    except DuplicateApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ApplicationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return ApplicationOut.model_validate(application)


@router.get("", response_model=list[ApplicationDetailOut])
def list_applications(
    status_filter: str = Query(default="", alias="status"),
    candidate_id: str = Query(default=""),
    job_posting_id: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[ApplicationDetailOut]:
    stmt = select(Application).where(Application.org_id == principal.org_id)
    if status_filter:
        if status_filter not in APPLICATION_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"unknown status {status_filter!r}",
            )
        stmt = stmt.where(Application.status == status_filter)
    if candidate_id:
        stmt = stmt.where(Application.candidate_id == candidate_id)
    if job_posting_id:
        stmt = stmt.where(Application.job_posting_id == job_posting_id)
    stmt = stmt.order_by(Application.updated_at.desc()).limit(limit).offset(offset)
    return [_detail(db, application) for application in db.scalars(stmt).all()]


@router.get("/count")
def count_applications(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> dict:
    rows = db.execute(
        select(Application.status, func.count())
        .where(Application.org_id == principal.org_id)
        .group_by(Application.status)
    ).all()
    return {"by_status": {row[0]: row[1] for row in rows}, "total": sum(r[1] for r in rows)}


@router.get("/{application_id}", response_model=ApplicationDetailOut)
def get_application(
    application_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ApplicationDetailOut:
    return _detail(db, _load(db, application_id, principal.org_id), with_history=True)


@router.patch("/{application_id}", response_model=ApplicationOut)
def patch_application(
    application_id: str,
    payload: ApplicationPatch,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> ApplicationOut:
    application = _load(db, application_id, principal.org_id)
    if payload.submission_url is not None:
        application.submission_url = payload.submission_url
    if payload.status_reason is not None:
        application.status_reason = payload.status_reason[:240]
    if payload.form_answers is not None:
        # A recruiter edit marks each touched answer; provenance survives so
        # the review trail still says where the original came from.
        application.form_answers = [
            {**answer, "edited": True} if answer.get("generated_by") else answer
            for answer in payload.form_answers
        ]
    db.flush()
    return ApplicationOut.model_validate(application)


@router.post("/{application_id}/transition", response_model=ApplicationOut)
def transition_application(
    application_id: str,
    payload: ApplicationTransitionIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> ApplicationOut:
    application = _load(db, application_id, principal.org_id)
    try:
        transition(
            db,
            application,
            to_status=payload.to_status,
            actor="recruiter",
            actor_id=principal.user_id,
            reason=payload.reason,
            occurred_at=payload.occurred_at,
        )
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return ApplicationOut.model_validate(application)


@router.get("/{application_id}/networking")
def networking_suggestions(
    application_id: str,
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> dict:
    from ..applications.networking import refresh_networking

    application = _load(db, application_id, principal.org_id)
    if refresh or not application.networking:
        entries = refresh_networking(db, application)
    else:
        entries = list(application.networking or [])
    return {"suggestions": entries}


@router.post("/{application_id}/networking/draft")
def draft_networking_intro(
    application_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> dict:
    from ..applications.networking import draft_intro

    application = _load(db, application_id, principal.org_id)
    contact_id = str(payload.get("contact_candidate_id") or "")
    try:
        entry = draft_intro(db, application, contact_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"entry": entry}


@router.post("/{application_id}/package", status_code=202)
def build_package(
    application_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> dict:
    application = _load(db, application_id, principal.org_id)
    try:
        job = request_package(db, application)
    except ApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"job_id": job.id, "status": job.status}

"""Recruiter-side application lifecycle API.

The candidate-side surface (portal, thread) reaches the same
``applications.service`` functions through its own principal; this router is
the recruiter's window — list, inspect, transition, rebuild packages. A
``TransitionError`` is a 409: the machine refused, nothing broke.
"""

from __future__ import annotations

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
from ..applications.submission import NotSubmittable, attempts_used, request_submission
from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..models import (
    APPLICATION_STATUSES,
    Application,
    ApplicationSubmission,
    Candidate,
    JobPosting,
)
from ..schemas import (
    ApplicationCreate,
    ApplicationDetailOut,
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


def _detail(db: Session, application: Application) -> ApplicationDetailOut:
    posting = db.get(JobPosting, application.job_posting_id)
    candidate = db.get(Candidate, application.candidate_id)
    documents = list(current_documents(db, application).values())
    out = ApplicationDetailOut.model_validate(application)
    out.company = posting.company if posting else ""
    out.title = posting.title if posting else ""
    out.candidate_name = candidate.full_name if candidate else ""
    out.documents = [CandidateDocumentOut.model_validate(doc) for doc in documents]
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
    return _detail(db, _load(db, application_id, principal.org_id))


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


@router.post("/{application_id}/submit", status_code=202)
def submit(
    application_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> dict:
    """Queue an automated attempt.

    A 409 here is the machine refusing for a reason worth reading: no recorded
    consent, the submission budget, the retry cap, or -- the common one -- no
    adapter for this employer's ATS, which means the application stays the
    assisted hand-off it already is.
    """
    application = _load(db, application_id, principal.org_id)
    try:
        job = request_submission(db, application, requested_by="recruiter")
    except NotSubmittable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {
        "job_id": job.id,
        "status": job.status,
        "attempts_used": attempts_used(db, application),
    }


@router.get("/{application_id}/submissions")
def list_submissions(
    application_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> dict:
    """Every attempt on this application, newest first.

    The recruiter-side answer to "what exactly did we send, and what happened":
    the vendor, the method, the verdict, the error, and the artifact paths a
    browser run left behind.
    """
    application = _load(db, application_id, principal.org_id)
    rows = db.scalars(
        select(ApplicationSubmission)
        .where(ApplicationSubmission.application_id == application.id)
        .order_by(ApplicationSubmission.attempt.desc())
    ).all()
    return {
        "submissions": [
            {
                "id": row.id,
                "attempt": row.attempt,
                "vendor": row.vendor,
                "method": row.method,
                "status": row.status,
                "external_id": row.external_id,
                "confirmation_ref": row.confirmation_ref,
                "error": row.error,
                "artifacts": list(row.artifacts or []),
                # The stored credential inside `verification` never leaves the
                # server; only the run log does.
                "run": (row.verification or {}).get("run", {}),
                "started_at": row.started_at,
                "finished_at": row.finished_at,
            }
            for row in rows
        ]
    }

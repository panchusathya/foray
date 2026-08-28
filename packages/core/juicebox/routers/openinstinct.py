"""Private API used by the separate OpenInstinct candidate deployment.

The caller is a short-lived signed service token, never a candidate or
recruiter login token.  This prevents either public application from treating
the other application's browser session as an authority.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..applications import limits
from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    create_application,
    current_documents,
    request_package,
)
from ..applications.state import TransitionError, transition
from ..config import get_settings
from ..deps import CommittingRoute, get_db
from ..models import (
    Application,
    ApplicationSubmission,
    Candidate,
    CandidateDocument,
    JobPosting,
    OpenInstinctApplicationTask,
    OpenInstinctIdentityLink,
    utcnow,
)
from ..openinstinct import (
    JUICEBOX_AUDIENCE,
    OpenInstinctError,
    OpenInstinctPrincipal,
    verify_bridge_token,
)

router = APIRouter(
    route_class=CommittingRoute,
    prefix="/v1/internal/openinstinct",
    tags=["openinstinct"],
)


def _service_principal(
    authorization: str | None = Header(default=None),
) -> OpenInstinctPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    try:
        return verify_bridge_token(authorization.split(" ", 1)[1].strip(), audience=JUICEBOX_AUDIENCE)
    except OpenInstinctError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


class IdentityLinkIn(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=160)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=64)


class ApplicationTaskIn(BaseModel):
    external_task_id: str = Field(min_length=1, max_length=160)
    job_posting_id: str = Field(min_length=1, max_length=40)
    conversation_url: str = Field(default="", max_length=500)


class ApplicationTaskResultIn(BaseModel):
    status: str = Field(pattern="^(submitted|needs_human|failed)$")
    vendor: str = Field(default="browser", max_length=32)
    external_id: str = Field(default="", max_length=200)
    confirmation_ref: str = Field(default="", max_length=400)
    error: str = Field(default="", max_length=600)
    artifacts: list[str] = Field(default_factory=list)


def _identity_value(principal: OpenInstinctPrincipal, *, kind: str) -> set[str]:
    return {
        item["value"].strip().lower()
        for item in principal.identities
        if item["kind"] == kind and item["value"].strip()
    }


def _link_for(db: Session, external_user_id: str) -> OpenInstinctIdentityLink:
    link = db.scalar(
        select(OpenInstinctIdentityLink).where(
            OpenInstinctIdentityLink.external_user_id == external_user_id
        )
    )
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OpenInstinct account is not linked")
    return link


def _task_for(db: Session, task_id: str, external_user_id: str) -> OpenInstinctApplicationTask:
    link = _link_for(db, external_user_id)
    task = db.scalar(
        select(OpenInstinctApplicationTask).where(
            OpenInstinctApplicationTask.id == task_id,
            OpenInstinctApplicationTask.org_id == link.org_id,
            OpenInstinctApplicationTask.candidate_id == link.candidate_id,
        )
    )
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such application task")
    return task


def _task_out(db: Session, task: OpenInstinctApplicationTask) -> dict:
    application = db.get(Application, task.application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application no longer exists")
    if task.status == "package_pending" and application.status == "prepared":
        decision = limits.may_submit(db, application=application)
        task.status = "ready" if decision else "needs_human"
        if not decision:
            task.result = {"error": f"submission budget: {decision.detail}"}
    docs = current_documents(db, application) if task.status == "ready" else {}
    return {
        "id": task.id,
        "external_task_id": task.external_task_id,
        "status": task.status,
        "application_id": application.id,
        "apply_url": application.submission_url,
        "form_answers": list(application.form_answers or []) if task.status == "ready" else [],
        "documents": [
            {
                "id": document.id,
                "filename": document.filename,
                "content_type": document.content_type,
                "download_url": (
                    f"{get_settings().base_url.rstrip('/')}"
                    f"/v1/internal/openinstinct/application-tasks/{task.id}/documents/{document.id}"
                ),
            }
            for document in docs.values()
        ],
        "result": dict(task.result or {}),
    }


@router.post("/identity-links", status_code=status.HTTP_201_CREATED)
def link_identity(
    payload: IdentityLinkIn,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    if payload.external_user_id != principal.subject:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Identity does not match service token")
    email = payload.email.strip().lower()
    phone = payload.phone.strip().lower()
    if (email and email not in _identity_value(principal, kind="email")) or (
        phone and phone not in _identity_value(principal, kind="phone")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Identity is not verified")
    if not email and not phone:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A verified email or phone is required")

    existing = db.scalar(
        select(OpenInstinctIdentityLink).where(
            OpenInstinctIdentityLink.external_user_id == payload.external_user_id
        )
    )
    candidates = [
        candidate
        for candidate in db.scalars(select(Candidate)).all()
        if (email and email in {str(value).strip().lower() for value in (candidate.emails or [])})
        or (phone and phone in {str(value).strip().lower() for value in (candidate.phones or [])})
    ]
    if len(candidates) != 1:
        detail = "No candidate matches this verified identity" if not candidates else "Identity matches multiple candidates"
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    candidate = candidates[0]
    if existing is not None:
        if existing.candidate_id != candidate.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OpenInstinct account is already linked")
        return {"org_id": existing.org_id, "candidate_id": existing.candidate_id, "linked": True}
    if db.scalar(
        select(OpenInstinctIdentityLink).where(
            OpenInstinctIdentityLink.org_id == candidate.org_id,
            OpenInstinctIdentityLink.candidate_id == candidate.id,
        )
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Candidate is already linked")
    link = OpenInstinctIdentityLink(
        org_id=candidate.org_id,
        candidate_id=candidate.id,
        external_user_id=payload.external_user_id,
        verified_email=email,
        verified_phone=phone,
    )
    db.add(link)
    db.flush()
    return {"org_id": link.org_id, "candidate_id": link.candidate_id, "linked": True}


@router.post("/application-tasks", status_code=status.HTTP_202_ACCEPTED)
def create_application_task(
    payload: ApplicationTaskIn,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    link = _link_for(db, principal.subject)
    existing = db.scalar(
        select(OpenInstinctApplicationTask).where(
            OpenInstinctApplicationTask.org_id == link.org_id,
            OpenInstinctApplicationTask.external_task_id == payload.external_task_id,
        )
    )
    if existing is not None:
        return _task_out(db, existing)
    posting = db.get(JobPosting, payload.job_posting_id)
    if posting is None or posting.org_id != link.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job posting not found")
    try:
        application = create_application(
            db,
            org_id=link.org_id,
            candidate_id=link.candidate_id,
            job_posting_id=posting.id,
            source="candidate",
        )
        request_package(db, application)
    except DuplicateApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    task = OpenInstinctApplicationTask(
        org_id=link.org_id,
        candidate_id=link.candidate_id,
        application_id=application.id,
        external_task_id=payload.external_task_id,
        conversation_url=payload.conversation_url,
    )
    db.add(task)
    db.flush()
    return _task_out(db, task)


@router.get("/application-tasks/{task_id}")
def get_application_task(
    task_id: str,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    return _task_out(db, _task_for(db, task_id, principal.subject))


@router.get("/application-tasks/{task_id}/documents/{document_id}")
def download_task_document(
    task_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> Response:
    task = _task_for(db, task_id, principal.subject)
    document = db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.id == document_id,
            CandidateDocument.application_id == task.application_id,
            CandidateDocument.org_id == task.org_id,
        )
    )
    if document is None or not document.storage_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return Response(
        storage.read_bytes(document.storage_path),
        media_type=document.content_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )


@router.post("/application-tasks/{task_id}/result")
def report_application_task(
    task_id: str,
    payload: ApplicationTaskResultIn,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    task = _task_for(db, task_id, principal.subject)
    application = db.get(Application, task.application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application no longer exists")
    if task.status in {"submitted", "needs_human", "failed"}:
        return _task_out(db, task)
    if task.status != "ready":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Application package is not ready")
    try:
        transition(db, application, to_status="submitting", actor="openinstinct")
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    attempt = ApplicationSubmission(
        org_id=task.org_id,
        application_id=application.id,
        attempt=1,
        vendor=payload.vendor,
        method="browser",
        status="succeeded" if payload.status == "submitted" else payload.status,
        external_id=payload.external_id,
        confirmation_ref=payload.confirmation_ref,
        error=payload.error,
        artifacts=list(payload.artifacts),
        started_at=utcnow(),
        finished_at=utcnow(),
    )
    db.add(attempt)
    task.status = payload.status
    task.result = payload.model_dump()
    task.reported_at = utcnow()
    if payload.status == "submitted":
        try:
            transition(db, application, to_status="submitted", actor="openinstinct")
        except TransitionError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    else:
        transition(db, application, to_status="prepared", actor="openinstinct", reason=payload.error)
    db.flush()
    return _task_out(db, task)

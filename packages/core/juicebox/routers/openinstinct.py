"""Private API used by the separate OpenInstinct candidate deployment.

The caller is a short-lived signed service token, never a candidate or
recruiter login token.  This prevents either public application from treating
the other application's browser session as an authority.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..applications import limits
from ..applications.bar import passes_bar
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
    CandidateEmail,
    CandidateDocument,
    CandidatePhone,
    JobMatch,
    JobPosting,
    JobProposal,
    IntakeMessage,
    Org,
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
from ..portal.schemas import portal_card
from ..presentation.jobcard import jobcard
from ..messaging.cards import card_png
from ..messaging.channels import DeliveryResult, OutboundMessage
from ..messaging.threads import find_by_provider_id, open_thread, record_inbound, record_outbound
from ..repository import merge_contact_details
from ..intake.phones import lookup_key

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
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=255)
    phone: str = Field(default="", max_length=64)


class ConversationEventIn(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=200)
    provider_native_id: str = Field(min_length=1, max_length=300)
    direction: str = Field(pattern="^(inbound|outbound)$")
    body: str = Field(min_length=1, max_length=20_000)
    channel: str = Field(default="linq", max_length=32)
    display_name: str = Field(default="", max_length=200)


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


@router.post("/resumes", status_code=status.HTTP_201_CREATED)
async def upload_resume(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    """Accept a candidate-selected resume from OpenInstinct.

    The service token resolves a single linked candidate.  Bytes land in the
    same candidate-document path as the portal, so parsing and application
    packaging have one source of truth.
    """
    from ..docgen.parse import SUFFIX_BY_MIME, sniff_resume_type
    from ..jobs import KIND_DOC_PARSE, enqueue
    from .documents import _read_capped

    link = _link_for(db, principal.subject)
    candidate = db.get(Candidate, link.candidate_id)
    if candidate is None or candidate.org_id != link.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found")

    data = await _read_capped(file)
    content_type = sniff_resume_type(data)
    if not content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF or Word (.docx) resumes are accepted",
        )
    suffix = SUFFIX_BY_MIME[content_type]
    db.execute(
        CandidateDocument.__table__.update()
        .where(
            CandidateDocument.candidate_id == candidate.id,
            CandidateDocument.kind == "resume_uploaded",
            CandidateDocument.is_default.is_(True),
        )
        .values(is_default=False, protected_default=False)
    )
    document = CandidateDocument(
        org_id=candidate.org_id,
        candidate_id=candidate.id,
        kind="resume_uploaded",
        is_default=True,
        protected_default=True,
        filename=(file.filename or f"resume{suffix}")[:255],
        content_type=content_type,
        status="uploaded",
        source="openinstinct_upload",
    )
    db.add(document)
    db.flush()
    relpath = storage.document_relpath(
        org_id=document.org_id,
        candidate_id=document.candidate_id,
        document_id=document.id,
        suffix=suffix,
    )
    size, digest = storage.save_bytes(relpath, data)
    document.storage_path = relpath
    document.size_bytes = size
    document.sha256 = digest
    enqueue(
        db,
        org_id=document.org_id,
        kind=KIND_DOC_PARSE,
        idempotency_key=f"{KIND_DOC_PARSE}:{document.id}",
        payload={"document_id": document.id},
    )
    return {"id": document.id, "filename": document.filename, "status": document.status}


@router.get("/resumes/default")
def download_default_resume(
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> Response:
    """Return the linked candidate's parsed protected default resume.

    This is deliberately a service-to-service byte response.  OpenInstinct's
    worker writes it directly into its owned Kernel filesystem, so neither a
    chat attachment URL nor document bytes are exposed to the model.
    """
    link = _link_for(db, principal.subject)
    document = db.scalar(
        select(CandidateDocument)
        .where(
            CandidateDocument.org_id == link.org_id,
            CandidateDocument.candidate_id == link.candidate_id,
            CandidateDocument.kind == "resume_uploaded",
            CandidateDocument.is_default.is_(True),
            CandidateDocument.protected_default.is_(True),
        )
        .order_by(CandidateDocument.created_at.desc())
    )
    if document is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No protected default resume is on this candidate profile. Attach a PDF or DOCX to continue.",
        )
    if document.status != "ready" or not document.storage_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The protected default resume is still being parsed. Try again once parsing finishes.",
        )
    return Response(
        storage.read_bytes(document.storage_path),
        media_type=document.content_type,
        headers={"Content-Disposition": f'attachment; filename="{document.filename}"'},
    )


@router.get("/job-feed")
def job_feed(
    q: str = Query(default="", max_length=120),
    location: str = Query(default="", max_length=120),
    limit: int = Query(default=5, ge=1, le=10),
    exclude_posting_id: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    """Return the linked candidate's current, actionable JuiceBox matches.

    The bridge projects the same candidate-safe cards as the portal feed: no
    recruiter score, no cross-org rows, and no roles the candidate has already
    dismissed or started pursuing.  OpenInstinct uses the returned posting id
    only when the candidate explicitly asks to apply to that concrete role.
    """
    link = _link_for(db, principal.subject)
    if len(exclude_posting_id) > 100:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Too many excluded postings")
    answered_postings = set(exclude_posting_id)
    answered_postings.update(
        db.scalars(
            select(JobProposal.job_posting_id).where(
                JobProposal.org_id == link.org_id,
                JobProposal.candidate_id == link.candidate_id,
            )
        ).all()
    )
    answered_postings.update(
        db.scalars(
            select(Application.job_posting_id).where(
                Application.org_id == link.org_id,
                Application.candidate_id == link.candidate_id,
            )
        ).all()
    )

    needle = q.strip().lower()
    place = location.strip().lower()
    rows = db.execute(
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_posting_id)
        .where(
            JobMatch.org_id == link.org_id,
            JobMatch.candidate_id == link.candidate_id,
            JobMatch.status != "dismissed",
            JobPosting.status == "open",
        )
        .order_by(JobMatch.score.desc())
        .limit(200)
    ).all()
    cards: list[dict] = []
    for match, posting in rows:
        if posting.id in answered_postings:
            continue
        if needle and needle not in f"{posting.title} {posting.company}".lower():
            continue
        if place and place not in (posting.location or "").lower():
            continue
        if not passes_bar(match, posting).ok:
            continue
        cards.append(portal_card(jobcard(posting, match)))
        if len(cards) >= limit:
            break
    return {"cards": cards}


@router.get("/job-cards/{posting_id}")
def job_card_png(
    posting_id: str,
    index: int = Query(default=1, ge=1, le=5),
    total: int = Query(default=1, ge=1, le=5),
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> Response:
    """Render a candidate-authorized card, never a recruiter-only score view."""
    if index > total:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Card index exceeds total")
    link = _link_for(db, principal.subject)
    pair = db.execute(
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_posting_id)
        .where(
            JobMatch.org_id == link.org_id,
            JobMatch.candidate_id == link.candidate_id,
            JobPosting.id == posting_id,
            JobMatch.status != "dismissed",
            JobPosting.status == "open",
        )
    ).first()
    if pair is None or not passes_bar(pair[0], pair[1]).ok:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job card is unavailable")
    already_answered = db.scalar(
        select(Application.id).where(
            Application.org_id == link.org_id,
            Application.candidate_id == link.candidate_id,
            Application.job_posting_id == posting_id,
        )
    ) or db.scalar(
        select(JobProposal.id).where(
            JobProposal.org_id == link.org_id,
            JobProposal.candidate_id == link.candidate_id,
            JobProposal.job_posting_id == posting_id,
        )
    )
    if already_answered:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job card is unavailable")
    return Response(
        card_png(
            db,
            org_id=link.org_id,
            posting=pair[1],
            card=portal_card(jobcard(pair[1], pair[0])),
            index=index,
            total=total,
        ),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


def _identity_value(principal: OpenInstinctPrincipal, *, kind: str) -> set[str]:
    return {
        item["value"].strip().lower()
        for item in principal.identities
        if item["kind"] == kind and item["value"].strip()
    }


def _verified_contact(principal: OpenInstinctPrincipal, kind: str) -> str:
    value = next(iter(_identity_value(principal, kind=kind)), "")
    return lookup_key(value) if kind == "phone" and value else value


def _matching_candidates(db: Session, *, email: str, phone: str) -> list[Candidate]:
    """Resolve contacts through their indexed projections, never a JSON table scan."""
    ids: set[str] = set()
    if email:
        ids.update(
            db.scalars(select(CandidateEmail.candidate_id).where(CandidateEmail.email == email)).all()
        )
    if phone:
        ids.update(
            db.scalars(select(CandidatePhone.candidate_id).where(CandidatePhone.phone == phone)).all()
        )
    if not ids:
        return []
    return list(db.scalars(select(Candidate).where(Candidate.id.in_(ids))).all())


def _link_candidate(
    db: Session,
    *,
    principal: OpenInstinctPrincipal,
    name: str = "",
) -> OpenInstinctIdentityLink:
    """Resolve one verified Foray identity or create it in the configured CRM org."""
    email = _verified_contact(principal, "email")
    phone = _verified_contact(principal, "phone")
    if not email and not phone:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A verified email or phone is required")

    existing = db.scalar(
        select(OpenInstinctIdentityLink).where(
            OpenInstinctIdentityLink.external_user_id == principal.subject
        )
    )
    if existing is not None:
        candidate = db.get(Candidate, existing.candidate_id)
        if candidate is None or candidate.org_id != existing.org_id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OpenInstinct link is stale")
        merge_contact_details(db, candidate, name=name, email=email, phone=phone)
        existing.verified_email = email or existing.verified_email
        existing.verified_phone = phone or existing.verified_phone
        db.flush()
        return existing

    matches = _matching_candidates(db, email=email, phone=phone)
    if len(matches) > 1:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Identity matches multiple candidates")
    candidate = matches[0] if matches else None
    if candidate is None:
        org_id = get_settings().openinstinct_default_org_id
        if not org_id or db.get(Org, org_id) is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="OpenInstinct default organization is not configured")
        fallback_name = (name or email or phone or "Foray candidate").strip()
        candidate = Candidate(
            org_id=org_id,
            source="openinstinct",
            source_id=principal.subject,
            full_name=fallback_name[:200],
            crm_tracked=True,
        )
        db.add(candidate)
        db.flush()
    merge_contact_details(db, candidate, name=name, email=email, phone=phone)
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
        external_user_id=principal.subject,
        verified_email=email,
        verified_phone=phone,
    )
    db.add(link)
    db.flush()
    return link


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
    link = _link_candidate(db, principal=principal, name=payload.name)
    return {"org_id": link.org_id, "candidate_id": link.candidate_id, "linked": True}


@router.post("/conversation-events", status_code=status.HTTP_202_ACCEPTED)
def record_conversation_event(
    payload: ConversationEventIn,
    db: Session = Depends(get_db),
    principal: OpenInstinctPrincipal = Depends(_service_principal),
) -> dict:
    """Mirror a delivered Foray message without invoking JuiceBox's intake agent."""
    link = _link_candidate(db, principal=principal, name=payload.display_name)
    thread = find_by_provider_id(
        db, provider="openinstinct", provider_thread_id=payload.conversation_id
    )
    if thread is None:
        thread = open_thread(
            db,
            org_id=link.org_id,
            candidate_id=link.candidate_id,
            channel="openinstinct",
            provider="openinstinct",
            provider_thread_id=payload.conversation_id,
            entry_point="openinstinct",
            from_address=_verified_contact(principal, "phone") or _verified_contact(principal, "email"),
            consent_messaging=True,
            contact_name=payload.display_name,
            contact_email=_verified_contact(principal, "email"),
            mode="managing",
        )
        thread.review_needed = False
        db.flush()
    elif thread.org_id != link.org_id or thread.candidate_id != link.candidate_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is already linked to another candidate")

    existing = db.scalar(
        select(IntakeMessage).where(
            IntakeMessage.thread_id == thread.id,
            IntakeMessage.provider_native_id == payload.provider_native_id,
        )
    )
    if existing is not None:
        return {"thread_id": thread.id, "message_id": existing.id, "created": False}
    if payload.direction == "inbound":
        message, created = record_inbound(
            db,
            thread=thread,
            body=payload.body,
            provider_native_id=payload.provider_native_id,
            raw={"channel": payload.channel, "source": "openinstinct"},
        )
        return {"thread_id": thread.id, "message_id": message.id, "created": created}
    message = record_outbound(
        db,
        thread=thread,
        message=OutboundMessage(body=payload.body, verbatim=True),
        result=DeliveryResult(status="sent", provider_native_id=payload.provider_native_id),
    )
    return {"thread_id": thread.id, "message_id": message.id, "created": True}


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

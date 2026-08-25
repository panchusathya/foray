"""The candidate portal API — everything apply.goforay.io renders.

Every handler depends on ``get_candidate_principal`` and filters
``org_id AND candidate_id`` in the WHERE clause — never load-then-check. The
portal's nginx proxies only ``/v1/portal/``, so from the candidate domain the
recruiter API does not exist; this router is the entire surface.

The feed is the swipe deck: bar-passing matches projected through the shared
jobcard contract, deduped against everything already proposed or pursued.
A swipe right is consent-to-pursue (a ``JobProposal`` acceptance plus an
Application and its package build); the package **approval** is the consent
to submit. A swipe left is remembered forever — a passed role never
resurfaces.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from pydantic import BaseModel
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
from ..crm import patch_candidate
from ..deps import CandidatePrincipal, CommittingRoute, get_candidate_principal, get_db
from ..models import (
    Application,
    Candidate,
    CandidateDocument,
    CandidatePreference,
    JobMatch,
    JobPosting,
    JobProposal,
    record_event,
    utcnow,
)
from ..portal.schemas import (
    PortalApplicationOut,
    PortalDocumentOut,
    PortalPreferenceOut,
    PortalProfileOut,
    PortalProfilePatch,
    portal_card,
)
from ..presentation.jobcard import jobcard
from ..security import signed_intake_token, verify_intake_token

logger = logging.getLogger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/portal", tags=["portal"])

FEED_LIMIT = 20
PURPOSE_DOC_SIG = "portal-doc"
DOC_SIG_TTL_SECONDS = 60

_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def _candidate(db: Session, principal: CandidatePrincipal) -> Candidate:
    candidate = db.scalar(
        select(Candidate).where(
            Candidate.id == principal.candidate_id, Candidate.org_id == principal.org_id
        )
    )
    if candidate is None:
        raise _NOT_FOUND
    return candidate


@router.get("/me")
def me(
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    candidate = _candidate(db, principal)
    preference = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
    )
    return {
        "candidate": PortalProfileOut.from_candidate(candidate).model_dump(),
        "preference": (
            PortalPreferenceOut.from_preference(preference).model_dump()
            if preference is not None
            else None
        ),
    }


@router.patch("/profile")
def patch_profile(
    payload: PortalProfilePatch,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """Candidate self-edits ride the same ``patch_candidate`` recruiters use,
    so the edit lands in ``manual_fields`` and survives the nightly refresh
    exactly like a recruiter's correction would."""
    candidate = _candidate(db, principal)
    fields = {key: value for key, value in payload.model_dump().items() if value is not None}
    if fields:
        patch_candidate(db, candidate=candidate, fields=fields)
        record_event(
            db,
            org_id=principal.org_id,
            type="candidate.self_updated",
            subject_type="candidate",
            subject_id=candidate.id,
            payload={"fields": sorted(fields)},
        )
    return {"candidate": PortalProfileOut.from_candidate(candidate).model_dump()}


# ---------------------------------------------------------------------------
# the feed (the swipe deck)
# ---------------------------------------------------------------------------


@router.get("/feed")
def feed(
    q: str = Query(default=""),
    location: str = Query(default=""),
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """Bar-passing matches, best first, minus everything already answered."""
    answered_postings = set(
        db.scalars(
            select(JobProposal.job_posting_id).where(
                JobProposal.org_id == principal.org_id,
                JobProposal.candidate_id == principal.candidate_id,
            )
        ).all()
    )
    answered_postings.update(
        db.scalars(
            select(Application.job_posting_id).where(
                Application.org_id == principal.org_id,
                Application.candidate_id == principal.candidate_id,
            )
        ).all()
    )

    stmt = (
        select(JobMatch, JobPosting)
        .join(JobPosting, JobPosting.id == JobMatch.job_posting_id)
        .where(
            JobMatch.org_id == principal.org_id,
            JobMatch.candidate_id == principal.candidate_id,
            JobMatch.status != "dismissed",
            JobPosting.status == "open",
        )
        .order_by(JobMatch.score.desc())
        .limit(200)
    )
    needle = (q or "").strip().lower()
    place = (location or "").strip().lower()

    cards: list[dict] = []
    for match, posting in db.execute(stmt).all():
        if posting.id in answered_postings:
            continue
        if needle and needle not in f"{posting.title} {posting.company}".lower():
            continue
        if place and place not in (posting.location or "").lower():
            continue
        decision = passes_bar(match, posting)
        if not decision.ok:
            continue
        cards.append(portal_card(jobcard(posting, match)))
        if len(cards) >= FEED_LIMIT:
            break
    return {"cards": cards}


class SwipeIn(BaseModel):
    direction: str  # right | left


@router.post("/feed/{posting_id}/swipe")
def swipe(
    posting_id: str,
    payload: SwipeIn,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    if payload.direction not in {"right", "left"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="direction must be 'right' or 'left'",
        )
    posting = db.scalar(
        select(JobPosting).where(
            JobPosting.id == posting_id, JobPosting.org_id == principal.org_id
        )
    )
    if posting is None:
        raise _NOT_FOUND

    existing = db.scalar(
        select(JobProposal).where(
            JobProposal.candidate_id == principal.candidate_id,
            JobProposal.job_posting_id == posting_id,
        )
    )
    if existing is not None:
        # A swipe already answered stays answered -- the deck deduped it, so
        # this is a stale client, not a change of heart.
        return {"status": existing.status, "duplicate": True}

    match = db.scalar(
        select(JobMatch).where(
            JobMatch.candidate_id == principal.candidate_id,
            JobMatch.job_posting_id == posting_id,
        )
    )
    now = utcnow()
    proposal = JobProposal(
        org_id=principal.org_id,
        candidate_id=principal.candidate_id,
        job_posting_id=posting_id,
        job_match_id=match.id if match is not None else None,
        channel="portal",
        status="accepted" if payload.direction == "right" else "declined",
        responded_at=now,
        decline_reason="swiped_left" if payload.direction == "left" else "",
    )
    db.add(proposal)
    db.flush()

    if payload.direction == "left":
        return {"status": "declined"}

    # The weekly proposal budget applies to a pulled feed too -- a fast thumb
    # must not do what consent forbids one at a time. Over budget, the
    # acceptance is parked: recorded, visible, not yet an application.
    decision = limits.may_propose(
        db,
        org_id=principal.org_id,
        candidate_id=principal.candidate_id,
        in_new_message=False,
    )
    if not decision:
        return {"status": "parked", "reason": decision.reason, "detail": decision.detail}

    try:
        application = create_application(
            db,
            org_id=principal.org_id,
            candidate_id=principal.candidate_id,
            job_posting_id=posting_id,
            source="candidate",
            proposal_id=proposal.id,
        )
    except (DuplicateApplicationError, ApplicationError) as exc:
        return {"status": "parked", "reason": "application-limit", "detail": str(exc)}
    job = request_package(db, application)
    return {"status": "accepted", "application_id": application.id, "package_job_id": job.id}


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------


def _application(db: Session, principal: CandidatePrincipal, application_id: str) -> Application:
    application = db.scalar(
        select(Application).where(
            Application.id == application_id,
            Application.org_id == principal.org_id,
            Application.candidate_id == principal.candidate_id,
        )
    )
    if application is None:
        raise _NOT_FOUND
    return application


def _application_out(db: Session, application: Application) -> dict:
    posting = db.get(JobPosting, application.job_posting_id)
    documents = list(current_documents(db, application).values())
    return PortalApplicationOut.from_application(application, posting, documents).model_dump()


@router.get("/applications")
def list_applications(
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    rows = db.scalars(
        select(Application)
        .where(
            Application.org_id == principal.org_id,
            Application.candidate_id == principal.candidate_id,
        )
        .order_by(Application.updated_at.desc())
    ).all()
    return {"applications": [_application_out(db, row) for row in rows]}


@router.get("/applications/{application_id}")
def get_application(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    application = _application(db, principal, application_id)
    # The package was rendered before the candidate answered the follow-up the
    # package itself prompted, so a blank comp answer here may already have an
    # answer on the preference row. Fold those in on read rather than leaving
    # the candidate looking at a form field they filled in yesterday by text.
    from ..applications.followups import refill_from_preference

    refill_from_preference(db, application)
    return _application_out(db, application)


@router.post("/applications/{application_id}/approve")
def approve_package(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """The per-application yes. Approval is what unlocks ``submitted``."""
    application = _application(db, principal, application_id)
    if application.status not in {"prepared", "awaiting_consent"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"nothing to approve in status {application.status!r}",
        )
    application.approved_at = utcnow()
    if application.status == "prepared":
        transition(db, application, to_status="awaiting_consent", actor="candidate")

    # Approval is the per-application yes, so this is the moment an automated
    # attempt becomes possible. Most postings will not have an adapter and the
    # answer is the hand-off we already do -- which is why `auto_submit` reports
    # a reason instead of raising.
    from ..applications.submission import auto_submit

    job, reason = auto_submit(db, application)
    out = _application_out(db, application)
    out["submission"] = {"queued": job is not None, "reason": reason}
    return out


@router.post("/applications/{application_id}/submit", status_code=202)
def submit_for_me(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """The explicit "you apply for me" tap.

    Distinct from ``confirm-submitted``, which is the candidate telling us they
    applied themselves. This one asks us to do it, and needs the same recorded
    approval every other path needs -- tapping this button *is* that approval
    when there is not one yet.
    """
    from ..applications.submission import NotSubmittable, request_submission

    application = _application(db, principal, application_id)
    if application.approved_at is None:
        application.approved_at = utcnow()
        db.flush()
    try:
        job = request_submission(db, application, requested_by="candidate")
    except NotSubmittable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ApplicationError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"job_id": job.id, "status": job.status}


@router.post("/applications/{application_id}/confirm-submitted")
def confirm_submitted(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """The one-tap 'I applied' after the Tier-0 hand-off."""
    application = _application(db, principal, application_id)
    application.submission_method = "portal_link"
    try:
        transition(db, application, to_status="submitted", actor="candidate")
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _application_out(db, application)


@router.post("/applications/{application_id}/withdraw")
def withdraw(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    application = _application(db, principal, application_id)
    try:
        transition(db, application, to_status="withdrawn", actor="candidate")
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _application_out(db, application)


@router.get("/applications/{application_id}/networking")
def networking(
    application_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    from ..applications.networking import refresh_networking

    application = _application(db, principal, application_id)
    entries = list(application.networking or []) or refresh_networking(db, application)
    # linkedin_url is public-profile data; internal ranking stays internal.
    safe = [
        {
            "candidate_id": entry.get("candidate_id", ""),
            "name": entry.get("name", ""),
            "title": entry.get("title", ""),
            "company": entry.get("company", ""),
            "basis": entry.get("basis", ""),
            "linkedin_url": entry.get("linkedin_url", ""),
            "draft": entry.get("draft", ""),
        }
        for entry in entries
    ]
    return {"suggestions": safe}


@router.post("/applications/{application_id}/networking/draft")
def networking_draft(
    application_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    from ..applications.networking import draft_intro

    application = _application(db, principal, application_id)
    try:
        entry = draft_intro(db, application, str(payload.get("contact_candidate_id") or ""))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    return {"draft": entry.get("draft", "")}


# ---------------------------------------------------------------------------
# documents
# ---------------------------------------------------------------------------


@router.get("/documents")
def list_documents(
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    rows = db.scalars(
        select(CandidateDocument)
        .where(
            CandidateDocument.org_id == principal.org_id,
            CandidateDocument.candidate_id == principal.candidate_id,
            CandidateDocument.kind.in_(("resume_uploaded", "resume_generated", "cover_letter")),
        )
        .order_by(CandidateDocument.created_at.desc())
    ).all()
    return {"documents": [PortalDocumentOut.model_validate(row).model_dump() for row in rows]}


def _document(db: Session, principal: CandidatePrincipal, document_id: str) -> CandidateDocument:
    doc = db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.id == document_id,
            CandidateDocument.org_id == principal.org_id,
            CandidateDocument.candidate_id == principal.candidate_id,
        )
    )
    if doc is None:
        raise _NOT_FOUND
    return doc


@router.post("/documents/{document_id}/download-url")
def document_download_url(
    document_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """A 60-second HMAC-signed URL for plain <a>/new-tab access, so the
    session token never appears in a URL. The track.py pattern."""
    doc = _document(db, principal, document_id)
    expires = int(time.time()) + DOC_SIG_TTL_SECONDS
    sig = signed_intake_token(PURPOSE_DOC_SIG, f"{doc.id}:{expires}")
    return {
        "url": f"/v1/portal/documents/{doc.id}/file?exp={expires}&sig={sig}",
        "expires_in": DOC_SIG_TTL_SECONDS,
    }


@router.get("/documents/{document_id}/file")
def document_file(
    document_id: str,
    exp: int = Query(default=0),
    sig: str = Query(default=""),
    download: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Response:
    """Signed-URL access — no session header, which is the point.

    The signature covers the document id AND the expiry, so neither can be
    swapped; an expired link is a fresh POST away, never worth extending.
    """
    if exp < int(time.time()) or not verify_intake_token(
        PURPOSE_DOC_SIG, f"{document_id}:{exp}", sig
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Link expired"
        )
    doc = db.get(CandidateDocument, document_id)
    if doc is None or not doc.storage_path:
        raise _NOT_FOUND
    try:
        data = storage.read_bytes(doc.storage_path)
    except FileNotFoundError as exc:
        raise _NOT_FOUND from exc
    disposition = "attachment" if download else "inline"
    filename = (doc.filename or f"{doc.id}.pdf").replace('"', "")
    return Response(
        content=data,
        media_type=doc.content_type or "application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )

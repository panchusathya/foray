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
from ..applications.inbox import TRACKER_KINDS, repliable_address
from ..applications.service import (
    ApplicationError,
    DuplicateApplicationError,
    create_application,
    current_documents,
    request_package,
)
from ..applications.state import TransitionError, transition
from ..applications.submit.base import METHOD_CONSOLE, METHOD_MEMORY
from ..crm import patch_candidate
from ..deps import CandidatePrincipal, CommittingRoute, get_candidate_principal, get_db
from ..models import (
    Application,
    ApplicationMessage,
    ApplicationSubmission,
    AtsAccount,
    Candidate,
    CandidateDocument,
    CandidatePreference,
    JobMatch,
    JobPosting,
    JobProposal,
    record_event,
    utcnow,
)
from ..outreach.sender import OutboundEmail, sender_for_org
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

    if payload.direction == "left":
        db.add(
            JobProposal(
                org_id=principal.org_id,
                candidate_id=principal.candidate_id,
                job_posting_id=posting_id,
                job_match_id=match.id if match is not None else None,
                channel="portal",
                status="declined",
                responded_at=now,
                decline_reason="swiped_left",
            )
        )
        db.flush()
        return {"status": "declined"}

    # Budget *before* inserting this row: `may_propose` counts every proposal
    # including one just flushed, so checking after would park the legal last
    # swipe of the week as over budget.
    budget = limits.may_propose(
        db,
        org_id=principal.org_id,
        candidate_id=principal.candidate_id,
        in_new_message=False,
    )
    bar = (
        passes_bar(match, posting)
        if match is not None
        else None
    )

    proposal = JobProposal(
        org_id=principal.org_id,
        candidate_id=principal.candidate_id,
        job_posting_id=posting_id,
        job_match_id=match.id if match is not None else None,
        channel="portal",
        status="accepted",
        responded_at=now,
    )
    db.add(proposal)
    db.flush()

    # Over budget, closed, or below the bar: the acceptance is parked —
    # recorded, visible, not yet an application. The feed already hid these,
    # but a stale client can still POST the id.
    if not budget:
        return {"status": "parked", "reason": budget.reason, "detail": budget.detail}
    if bar is None or not bar.ok:
        why = "no-match" if bar is None else bar.why_not
        return {"status": "parked", "reason": why, "detail": why}

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
    return PortalApplicationOut.from_application(
        application, posting, documents, can_auto_submit=_can_auto_submit(db, application, posting)
    ).model_dump()


def _can_auto_submit(db: Session, application: Application, posting: JobPosting | None) -> bool:
    """Could we send this one ourselves, right now?

    Same two questions ``request_submission`` asks, so the button appears
    exactly when pressing it would work: is there an adapter for this employer's
    ATS, and is this candidate past the first-application review.
    """
    if posting is None or application.status not in {"prepared", "awaiting_consent"}:
        return False
    from ..applications.ats import detect
    from ..applications.submission import first_submission_gate, submitter_for

    if first_submission_gate(db, application):
        return False
    target = detect.target_of(posting)
    if not target:
        return False
    submitter, _ = submitter_for(db, application, posting, target)
    return submitter is not None


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


class AnswerPatchIn(BaseModel):
    answers: list[dict] = []


@router.patch("/applications/{application_id}/answers")
def patch_answers(
    application_id: str,
    payload: AnswerPatchIn,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """The candidate answers their own application's questions.

    This is the endpoint the follow-up text has been pointing at ("answer in
    the app and i'll include it"), and it is the *only* place an attestation --
    veteran status, disability, EEO self-ID -- can be answered at all: the
    package never generates one and the agent never asks one over SMS, so a
    required self-ID question is answerable exactly here, by the person it is
    about, or not at all.

    Existing keys only. A candidate can fill or reword any answer on their own
    application, but the question list is the form's, not theirs -- inventing a
    key here would put an unasked answer on the wire at submit time.
    """
    application = _application(db, principal, application_id)
    if application.status not in {"draft", "prepared", "awaiting_consent"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"answers are locked once submission starts ({application.status})",
        )

    updates = {
        str(item.get("key")): str(item.get("answer") or "").strip()
        for item in (payload.answers or [])
        if str(item.get("key") or "").strip()
    }
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Nothing to save."
        )

    answers = [dict(item) for item in (application.form_answers or [])]
    known = {str(item.get("key")) for item in answers}
    unknown = sorted(set(updates) - known)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"not questions on this application: {', '.join(unknown)}",
        )

    changed = []
    for item in answers:
        key = str(item.get("key"))
        if key not in updates or str(item.get("answer") or "") == updates[key]:
            continue
        item["answer"] = updates[key]
        # Their words, on the record as theirs. `edited` is also what shields
        # the answer from refill_from_preference and re-discovery merges.
        item["edited"] = True
        item["generated_by"] = "candidate"
        item["provenance"] = "candidate:portal"
        changed.append(key)

    if changed:
        application.form_answers = answers
        db.flush()
        record_event(
            db,
            org_id=principal.org_id,
            type="application.answers_edited",
            subject_type="application",
            subject_id=application.id,
            payload={"keys": changed},
        )
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
    # Stamp the method only after the move succeeds — a 409 (no consent,
    # illegal status) must not rewrite a browser/email attempt that is
    # already in flight, or a draft that never left.
    previous_method = application.submission_method or ""
    try:
        transition(db, application, to_status="submitted", actor="candidate")
    except TransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not previous_method:
        application.submission_method = "portal_link"
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
    has_default_upload = any(
        row.kind == "resume_uploaded" and row.is_default and row.storage_path for row in rows
    )
    if has_default_upload:
        rows = [
            row
            for row in rows
            if row.kind != "resume_generated"
        ]
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


# --- tracker ---------------------------------------------------------------
#
# What an employer actually said, rather than the status we inferred from it.
# The alias made us the addressee of record, so this inbox is the candidate's
# only view of correspondence that never reached their own mail client -- and
# the relay is best-effort, so it is also the durable one.


def _message_out(row: ApplicationMessage, posting: JobPosting | None) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "from_name": row.from_name or (row.from_address.partition("@")[2] or row.from_address),
        "from_address": row.from_address,
        "subject": row.subject,
        # A preview, not the body: the list renders hundreds of these and the
        # body of an ATS mail is mostly boilerplate and tracking pixels.
        "preview": " ".join((row.body or "").split())[:180],
        "received_at": row.received_at,
        "read": row.read_at is not None,
        "application_id": row.application_id or "",
        "company": (posting.company if posting else "") or "",
        "title": (posting.title if posting else "") or "",
        "direction": row.direction,
        # Empty means there is nobody behind this one -- a verification robot.
        # The UI shows that rather than a reply box that would bounce.
        "reply_to": repliable_address(row),
    }


@router.get("/tracker/messages")
def tracker_messages(
    kind: str = Query(default=""),
    application_id: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """The candidate's employer inbox, newest first.

    ``counts`` is computed over the whole inbox rather than the filtered page,
    because it drives the filter chips -- a count that changed when you tapped
    a chip would be describing the wrong thing.
    """
    where = [
        ApplicationMessage.org_id == principal.org_id,
        ApplicationMessage.candidate_id == principal.candidate_id,
    ]
    if kind:
        where.append(ApplicationMessage.kind == kind)
    if application_id:
        where.append(ApplicationMessage.application_id == application_id)

    rows = db.scalars(
        select(ApplicationMessage)
        .where(*where)
        .order_by(ApplicationMessage.received_at.desc())
        .limit(limit)
    ).all()

    postings: dict[str, JobPosting | None] = {}
    for row in rows:
        if row.application_id and row.application_id not in postings:
            application = db.get(Application, row.application_id)
            postings[row.application_id] = (
                db.get(JobPosting, application.job_posting_id) if application else None
            )

    counts: dict[str, int] = {}
    unread = 0
    for row in db.scalars(
        select(ApplicationMessage).where(
            ApplicationMessage.org_id == principal.org_id,
            ApplicationMessage.candidate_id == principal.candidate_id,
        )
    ).all():
        counts[row.kind] = counts.get(row.kind, 0) + 1
        counts["all"] = counts.get("all", 0) + 1
        if row.read_at is None:
            unread += 1

    return {
        "messages": [_message_out(row, postings.get(row.application_id or "")) for row in rows],
        "counts": counts,
        "unread": unread,
        "kinds": list(TRACKER_KINDS),
    }


@router.get("/tracker/messages/{message_id}")
def tracker_message(
    message_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """One message with its body. Reading it marks it read."""
    row = db.scalar(
        select(ApplicationMessage).where(
            ApplicationMessage.id == message_id,
            ApplicationMessage.org_id == principal.org_id,
            ApplicationMessage.candidate_id == principal.candidate_id,
        )
    )
    if row is None:
        raise _NOT_FOUND
    if row.read_at is None:
        row.read_at = utcnow()
        db.flush()
    application = db.get(Application, row.application_id) if row.application_id else None
    posting = db.get(JobPosting, application.job_posting_id) if application else None
    out = _message_out(row, posting)
    out["body"] = row.body or ""
    out["alias_address"] = row.alias_address
    out["relayed"] = row.relayed
    return out


@router.get("/tracker/pipeline")
def tracker_pipeline(
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """Everything in flight, with the last thing the employer said about it.

    In-flight means "we are still waiting on somebody else" -- a withdrawn or
    rejected pursuit is history, and burying the three live ones under thirty
    closed ones is how a tracker stops being read.
    """
    live = ("prepared", "awaiting_consent", "submitting", "submitted", "acknowledged")
    applications = db.scalars(
        select(Application)
        .where(
            Application.org_id == principal.org_id,
            Application.candidate_id == principal.candidate_id,
            Application.status.in_(live),
        )
        .order_by(Application.updated_at.desc())
    ).all()

    out = []
    for application in applications:
        posting = db.get(JobPosting, application.job_posting_id)
        last = db.scalar(
            select(ApplicationMessage)
            .where(ApplicationMessage.application_id == application.id)
            .order_by(ApplicationMessage.received_at.desc())
            .limit(1)
        )
        attempt = db.scalar(
            select(ApplicationSubmission)
            .where(ApplicationSubmission.application_id == application.id)
            .order_by(ApplicationSubmission.attempt.desc())
            .limit(1)
        )
        out.append(
            {
                "id": application.id,
                "status": application.status,
                "company": (posting.company if posting else "") or "",
                "title": (posting.title if posting else "") or "",
                "submitted_at": application.submitted_at,
                "submission_url": application.submission_url or "",
                # `console` and `memory` are dry runs. Saying "sent" for one
                # would be the same lie the notification suppression exists
                # to prevent.
                "was_really_sent": bool(
                    attempt
                    and attempt.status == "succeeded"
                    and attempt.method not in {METHOD_CONSOLE, METHOD_MEMORY}
                ),
                "vendor": (attempt.vendor if attempt else "") or "",
                "attempt_status": (attempt.status if attempt else "") or "",
                "last_message": _message_out(last, posting) if last is not None else None,
            }
        )
    return {"applications": out}


class TrackerReplyIn(BaseModel):
    body: str = ""


@router.post("/tracker/messages/{message_id}/reply", status_code=201)
def tracker_reply(
    message_id: str,
    payload: TrackerReplyIn,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """Answer an employer as the address the application was filed under.

    The candidate's own address never appears. They applied as
    ``anna+<token>@``, so the employer has no record of anything else, and a
    reply from their personal inbox arrives as a stranger on a thread they
    cannot match. Sending as the alias also means the employer's answer comes
    back to us, which is what keeps the tracker from going dark the moment a
    real conversation starts.
    """
    text = (payload.body or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Write something first."
        )

    row = db.scalar(
        select(ApplicationMessage).where(
            ApplicationMessage.id == message_id,
            ApplicationMessage.org_id == principal.org_id,
            ApplicationMessage.candidate_id == principal.candidate_id,
        )
    )
    if row is None:
        raise _NOT_FOUND

    target = repliable_address(row)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This one came from a no-reply address, so there is nobody to answer.",
        )

    alias_address = row.alias_address or ""
    if not alias_address:
        # Pre-alias mail. Sending from anywhere else would reach them as a
        # stranger, which is worse than telling the truth here.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This message predates your managed address, so we cannot answer as you.",
        )

    subject = row.subject or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    candidate = db.get(Candidate, principal.candidate_id)
    result = sender_for_org(db, principal.org_id).send(
        OutboundEmail(
            to=target,
            subject=subject,
            text_body=text,
            html_body="",
            from_email=alias_address,
            from_name=(candidate.full_name if candidate else "") or "Applications via Foray",
            # Back to the alias, so their answer is attributable to this
            # application rather than landing on the bare mailbox.
            headers={"Reply-To": alias_address},
            in_reply_to=row.rfc_message_id or "",
            references=row.rfc_message_id or "",
        )
    )
    if not result.ok:
        logger.warning("portal reply failed for %s: %s", message_id, result.error)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="We could not send that just now. Try again in a moment.",
        )

    sent = ApplicationMessage(
        org_id=principal.org_id,
        candidate_id=principal.candidate_id,
        application_id=row.application_id,
        direction="outbound",
        kind="reply",
        alias_address=alias_address,
        from_address=alias_address,
        from_name=(candidate.full_name if candidate else "") or "",
        subject=subject,
        body=text,
        provider_native_id=result.provider_native_id or "",
        received_at=utcnow(),
        read_at=utcnow(),
    )
    db.add(sent)
    db.flush()

    record_event(
        db,
        org_id=principal.org_id,
        type="application.reply_sent",
        subject_type="application",
        subject_id=row.application_id or row.candidate_id,
        payload={"message_id": sent.id, "to": target, "in_reply_to": row.id},
    )

    application = db.get(Application, row.application_id) if row.application_id else None
    posting = db.get(JobPosting, application.job_posting_id) if application else None
    return _message_out(sent, posting)


# ---------------------------------------------------------------------------
# ATS portal accounts: what exists in your name, and taking one over
# ---------------------------------------------------------------------------


@router.get("/accounts")
def list_ats_accounts(
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> list[dict]:
    """The employer-portal accounts registered in this candidate's name.

    The username is the alias -- an address, not a secret. The password is
    never in any response body: taking the account over goes through the
    board's own reset (the mail arrives at the alias and is relayed), so the
    credential the candidate ends up with is one we have never seen.
    """
    rows = db.scalars(
        select(AtsAccount)
        .where(
            AtsAccount.org_id == principal.org_id,
            AtsAccount.candidate_id == principal.candidate_id,
        )
        .order_by(AtsAccount.created_at.desc())
    ).all()
    return [
        {
            "id": row.id,
            "vendor": row.vendor,
            "tenant": row.tenant,
            "username": row.username,
            "status": row.status,
            "created_at": row.created_at,
            "handed_over": row.handed_over_at is not None,
        }
        for row in rows
    ]


@router.post("/accounts/{account_id}/handover")
def hand_over_ats_account(
    account_id: str,
    db: Session = Depends(get_db),
    principal: CandidatePrincipal = Depends(get_candidate_principal),
) -> dict:
    """Give the candidate sole control of one portal account.

    Marks the row handed over -- ``stored_account`` stops offering it to
    submit runs immediately -- and tells them how to claim it: run the
    board's own "forgot password" against the username shown, and the reset
    mail lands at the alias, which relays to their real address. No secret
    moves through this response, and the password they set is theirs alone.

    Idempotent: handing over twice is the same handover.
    """
    row = db.scalar(
        select(AtsAccount).where(
            AtsAccount.id == account_id,
            AtsAccount.org_id == principal.org_id,
            AtsAccount.candidate_id == principal.candidate_id,
        )
    )
    if row is None:
        raise _NOT_FOUND
    if row.handed_over_at is None:
        row.handed_over_at = utcnow()
        db.flush()
        record_event(
            db,
            org_id=principal.org_id,
            type="ats_account.handed_over",
            subject_type="ats_account",
            subject_id=row.id,
            payload={"vendor": row.vendor, "tenant": row.tenant},
        )
    return {
        "id": row.id,
        "vendor": row.vendor,
        "tenant": row.tenant,
        "username": row.username,
        "handed_over": True,
        "next_step": (
            "use the board's own 'forgot password' with this username -- the "
            "reset email arrives at your managed address and we'll forward it "
            "to you. the password you set is yours; we never see it and we "
            "stop using this account."
        ),
    }

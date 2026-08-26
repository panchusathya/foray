"""Creating applications and building their packages.

``create_application`` is the one door: every surface — the thread's
"apply to 2", a portal swipe, a recruiter click — comes through here, so the
per-posting uniqueness, the per-company budget, and consent capture cannot be
bypassed by one surface forgetting a check.

``build_package`` is the worker half: documents are files the moment they are
generated, but the Application's consent gate is what stands between a
package and a submission.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..docgen.answers import build_form_answers
from ..docgen.cover_letter import build_cover_letter
from ..docgen.render import render_letter_pdf, render_resume_pdf
from ..docgen.resume import RESUME_SCHEMA_VERSION, build_resume_json, default_uploaded_resume
from ..jobs import KIND_APP_PACKAGE, OPEN_STATUSES, Job, enqueue
from ..models import (
    Application,
    Candidate,
    CandidateDocument,
    CandidatePreference,
    JobMatch,
    JobPosting,
    record_event,
    utcnow,
)
from . import limits
from .state import transition

logger = logging.getLogger(__name__)


class ApplicationError(ValueError):
    """A refusal a router should surface, not a bug."""


class DuplicateApplicationError(ApplicationError):
    """One application per (candidate, posting), ever."""


def create_application(
    db: Session,
    *,
    org_id: str,
    candidate_id: str,
    job_posting_id: str,
    source: str = "candidate",
    created_by: str | None = None,
    job_match_id: str | None = None,
    proposal_id: str | None = None,
    consenting_message_id: str | None = None,
    consent_verbatim: str = "",
) -> Application:
    posting = db.get(JobPosting, job_posting_id)
    if posting is None or posting.org_id != org_id:
        raise ApplicationError("unknown job posting")
    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.org_id != org_id:
        raise ApplicationError("unknown candidate")

    existing = db.scalar(
        select(Application).where(
            Application.candidate_id == candidate_id,
            Application.job_posting_id == job_posting_id,
        )
    )
    if existing is not None:
        raise DuplicateApplicationError(
            f"an application to this posting already exists ({existing.id})"
        )

    decision = limits.may_create_application(
        db, org_id=org_id, candidate_id=candidate_id, posting=posting
    )
    if not decision:
        raise ApplicationError(decision.detail)

    if job_match_id is None:
        match = db.scalar(
            select(JobMatch).where(
                JobMatch.job_posting_id == job_posting_id,
                JobMatch.candidate_id == candidate_id,
            )
        )
        job_match_id = match.id if match is not None else None

    application = Application(
        org_id=org_id,
        candidate_id=candidate_id,
        job_posting_id=job_posting_id,
        job_match_id=job_match_id,
        proposal_id=proposal_id,
        project_id=posting.project_id,
        source=source,
        # The resolved form, not the listing we found it on. `apply_url` is
        # whatever ats/detect.py landed on and falls back to source_url, so a
        # posting nobody has resolved yet behaves exactly as before.
        submission_url=posting.apply_url or posting.source_url or "",
        consenting_message_id=consenting_message_id,
        consent_verbatim=(consent_verbatim or "")[:400],
        created_by=created_by,
    )
    db.add(application)
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="application.created",
        subject_type="application",
        subject_id=application.id,
        actor_id=created_by,
        payload={"source": source, "job_posting_id": job_posting_id},
    )
    return application


def request_package(db: Session, application: Application) -> Job:
    """Queue a package build. Collapses double-clicks via the open-job guard;
    a deliberate rebuild after completion gets a fresh timestamped key."""
    if application.status not in {"draft", "prepared"}:
        raise ApplicationError(
            f"cannot build a package for an application in {application.status!r}"
        )
    open_jobs = db.scalars(
        select(Job).where(
            Job.org_id == application.org_id,
            Job.kind == KIND_APP_PACKAGE,
            Job.status.in_(OPEN_STATUSES),
        )
    ).all()
    for job in open_jobs:
        if (job.payload or {}).get("application_id") == application.id:
            return job
    return enqueue(
        db,
        org_id=application.org_id,
        kind=KIND_APP_PACKAGE,
        idempotency_key=f"{KIND_APP_PACKAGE}:{application.id}:{int(utcnow().timestamp())}",
        payload={"application_id": application.id},
    )


def build_package(db: Session, application: Application) -> dict[str, Any]:
    """Worker side: resume, cover letter, form answers, then → prepared."""
    candidate = db.get(Candidate, application.candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if candidate is None or posting is None:
        raise ApplicationError("application lost its candidate or posting")

    uploaded = default_uploaded_resume(db, candidate.id)
    if uploaded is not None and not uploaded.storage_path:
        raise ApplicationError("the uploaded resume is still processing")

    # A resume with no employment history is not a resume. The onboarding
    # scrape only learns a current title and a rough tenure, so a candidate
    # who came in that way has nothing for the generator to work from -- this
    # is the one moment worth paying the provider for the full history
    # (enrich.profile_sources). Inline rather than queued: we are already in a
    # worker job, and a package built now with a blank career cannot be
    # un-sent later.
    if uploaded is None and not candidate.experiences and candidate.linkedin_url:
        from ..enrich.profile_sources import paid_lookup

        _, error = paid_lookup(
            db,
            org_id=application.org_id,
            linkedin_url=candidate.linkedin_url,
            candidate=candidate,
        )
        if error:
            logger.info(
                "package for %s proceeding without fetched history: %s",
                application.id,
                error,
            )
        db.refresh(candidate)

    preference = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
    )
    uploaded_text = uploaded.text_extracted if uploaded is not None else ""

    try:
        resume_json, resume_by = build_resume_json(
            db,
            candidate,
            preference=preference,
            posting=posting,
            uploaded_text=uploaded_text,
        )
        if uploaded is not None:
            # Do not re-render or rewrite a candidate-authored resume. The
            # original bytes, filename, and content type remain the attachment.
            resume_doc = uploaded
        else:
            resume_doc = _write_document(
                db,
                application=application,
                kind="resume_generated",
                structured=resume_json,
                generated_by=resume_by,
                pdf=render_resume_pdf(resume_json),
                filename=_document_name(candidate, posting, "resume"),
            )

        letter_body, letter_by = build_cover_letter(resume_json, posting)
        letter_doc = _write_document(
            db,
            application=application,
            kind="cover_letter",
            structured={"body": letter_body},
            generated_by=letter_by,
            pdf=render_letter_pdf(letter_body, candidate_name=candidate.full_name or ""),
            filename=_document_name(candidate, posting, "cover-letter"),
        )

        answers, _ = build_form_answers(
            preference=preference, resume_json=resume_json, posting=posting
        )
        # The form's own questions, where a browser can read them. Best-effort
        # by design: the standard set above is a complete package on its own.
        from .questions import augment_with_form_questions

        answers, discovery = augment_with_form_questions(
            db, application=application, posting=posting, answers=answers
        )
        application.form_answers = answers
        application.package_error = ""
    except Exception as exc:
        # Recorded on the row so the recruiter surface can show it; re-raised
        # so the queue's backoff retries the build.
        application.package_error = str(exc)[:400]
        db.flush()
        raise

    # Rebuilds are allowed from prepared (request_package says so). prepared →
    # prepared is not a legal move, and a no-op transition would park the
    # worker job forever after the package itself had already been rewritten.
    if application.status != "prepared":
        transition(db, application, to_status="prepared", actor="agent")
    return {
        "application_id": application.id,
        "resume_document_id": resume_doc.id,
        "cover_letter_document_id": letter_doc.id,
        "answers": len(application.form_answers or []),
        # "ran" or "skipped: <reason>" -- whether the questions the candidate
        # will review came from the employer's actual form or only our set.
        "discovery": discovery,
    }


def _write_document(
    db: Session,
    *,
    application: Application,
    kind: str,
    structured: dict,
    generated_by: str,
    pdf: bytes,
    filename: str,
) -> CandidateDocument:
    doc = CandidateDocument(
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        application_id=application.id,
        kind=kind,
        filename=filename,
        content_type="application/pdf",
        status="ready",
        structured=structured,
        schema_version=RESUME_SCHEMA_VERSION if kind == "resume_generated" else "",
        generated_by=generated_by,
        source="generated",
    )
    db.add(doc)
    db.flush()
    relpath = storage.document_relpath(
        org_id=doc.org_id, candidate_id=doc.candidate_id, document_id=doc.id
    )
    size, digest = storage.save_bytes(relpath, pdf)
    doc.storage_path = relpath
    doc.size_bytes = size
    doc.sha256 = digest
    db.flush()
    return doc


def _document_name(candidate: Candidate, posting: JobPosting, suffix: str) -> str:
    name = (candidate.full_name or "candidate").strip().replace(" ", "-").lower()
    company = (posting.company_normalized or posting.company or "role").strip().replace(" ", "-")
    return f"{name}-{company}-{suffix}.pdf"[:255]


def current_documents(db: Session, application: Application) -> dict[str, CandidateDocument]:
    """The latest ready variant per kind, falling back to the profile upload."""
    rows = db.scalars(
        select(CandidateDocument)
        .where(
            CandidateDocument.application_id == application.id,
            CandidateDocument.status == "ready",
        )
        .order_by(CandidateDocument.created_at)
    ).all()
    documents = {row.kind: row for row in rows}
    uploaded = default_uploaded_resume(db, application.candidate_id)
    if uploaded is not None and uploaded.storage_path:
        # An upload made after an earlier package was generated still wins at
        # submission time. Keep the old artifact for audit, but never attach it.
        documents.pop("resume_generated", None)
        documents["resume_uploaded"] = uploaded
    return documents


def application_status_summary(db: Session, *, org_id: str, candidate_id: str) -> str:
    """One short paragraph for the thread: where every open pursuit stands."""
    rows = list(
        db.scalars(
            select(Application)
            .where(
                Application.org_id == org_id,
                Application.candidate_id == candidate_id,
            )
            .order_by(Application.updated_at.desc())
            .limit(5)
        ).all()
    )
    if not rows:
        return "No applications in flight yet."
    described = {
        "draft": "being prepared",
        "prepared": "package ready for your review",
        "awaiting_consent": "waiting on your OK",
        "submitting": "being sent now",
        "submitted": "submitted",
        "acknowledged": "received by the employer",
        "interviewing": "interviewing",
        "offer": "offer stage",
        "rejected": "closed (not moving forward)",
        "withdrawn": "withdrawn",
    }
    lines = []
    for application in rows:
        posting = db.get(JobPosting, application.job_posting_id)
        label = f"{posting.title} at {posting.company}" if posting else "a role"
        lines.append(f"{label}: {described.get(application.status, application.status)}")
    return "; ".join(lines)

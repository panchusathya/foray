"""The profile-level ATS resume: one clean PDF per candidate, kept current.

Dropping a LinkedIn URL (or a resume file) into the intake conversation
promises the person a usable artifact, not just a row in a CRM. This module
keeps that promise: whenever a promotion lands new facts — a paid profile
fetch, a parsed upload, an identity link — the worker refreshes ONE
``resume_generated`` document with ``application_id`` NULL. The per-role
tailored variants that ``applications.service`` writes are siblings, not
replacements; this is the resume the candidate can see and download before
any application exists.

Two rules:

* **Upsert, never accumulate.** The portal lists documents; a regeneration
  per promote event would bury the person's own upload under copies of ours.
  One row per candidate, updated in place, same id forever.
* **Silence is worse than a thin resume, but a blank one is worst.** A
  candidate with no employment history, no summary and no skills gets no
  document — a name-and-headline PDF asserted under somebody's name helps
  nobody. When the candidate has a LinkedIn URL and no history, this is the
  onboarding-time version of ``build_package``'s "one moment worth paying":
  buy the history, because the artifact was the promise.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..models import Candidate, CandidateDocument, CandidatePreference
from .render import render_resume_pdf
from .resume import RESUME_SCHEMA_VERSION, build_resume_json

logger = logging.getLogger(__name__)

SKIPPED_EMPTY = "nothing to assert yet (no history, summary, or skills)"


def profile_resume_of(db: Session, candidate_id: str) -> CandidateDocument | None:
    """The candidate's one profile-level generated resume, if it exists."""
    return db.scalar(
        select(CandidateDocument).where(
            CandidateDocument.candidate_id == candidate_id,
            CandidateDocument.kind == "resume_generated",
            CandidateDocument.application_id.is_(None),
        )
    )


def refresh_profile_resume(
    db: Session, candidate: Candidate
) -> tuple[CandidateDocument | None, str]:
    """Regenerate the profile resume from what the CRM now knows.

    Returns ``(document, error)``. ``(None, reason)`` means there was nothing
    worth generating; a document with a non-empty ``error`` means generation
    failed and the row says so — the next promote event retries by design, so
    the failure is recorded rather than raised.
    """
    if not candidate.experiences and (candidate.linkedin_url or "").strip():
        from ..enrich.profile_sources import paid_lookup

        _, error = paid_lookup(
            db,
            org_id=candidate.org_id,
            linkedin_url=candidate.linkedin_url,
            candidate=candidate,
        )
        if error:
            logger.info(
                "profile resume for %s proceeding without fetched history: %s",
                candidate.id,
                error,
            )
        db.refresh(candidate)

    if not candidate.experiences and not (candidate.summary or "").strip() and not (
        candidate.skills or []
    ):
        return None, SKIPPED_EMPTY

    preference = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == candidate.id)
    )

    doc = profile_resume_of(db, candidate.id)
    try:
        resume_json, generated_by = build_resume_json(db, candidate, preference=preference)
        pdf = render_resume_pdf(resume_json)
    except Exception as exc:  # noqa: BLE001 - recorded on the row, regenerated next event
        message = str(exc)[:400]
        logger.warning("profile resume build failed for %s: %s", candidate.id, message)
        if doc is None:
            doc = CandidateDocument(
                org_id=candidate.org_id,
                candidate_id=candidate.id,
                kind="resume_generated",
                filename=_filename(candidate),
                content_type="application/pdf",
                source="generated",
            )
            db.add(doc)
        doc.status = "failed"
        doc.error = message
        db.flush()
        return doc, message

    if doc is None:
        doc = CandidateDocument(
            org_id=candidate.org_id,
            candidate_id=candidate.id,
            kind="resume_generated",
            content_type="application/pdf",
            source="generated",
        )
        db.add(doc)
        db.flush()

    doc.filename = _filename(candidate)
    doc.structured = resume_json
    doc.schema_version = RESUME_SCHEMA_VERSION
    doc.generated_by = generated_by
    doc.status = "ready"
    doc.error = ""

    relpath = doc.storage_path or storage.document_relpath(
        org_id=doc.org_id, candidate_id=doc.candidate_id, document_id=doc.id
    )
    size, digest = storage.save_bytes(relpath, pdf)
    doc.storage_path = relpath
    doc.size_bytes = size
    doc.sha256 = digest
    db.flush()
    return doc, ""


def _filename(candidate: Candidate) -> str:
    name = (candidate.full_name or "candidate").strip().replace(" ", "-").lower()
    return f"{name}-resume.pdf"[:255]

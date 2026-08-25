"""Sending an application: the one door in, and the worker half.

``request_submission`` is the only way an automated attempt starts. Consent and
the submission budget are checked *here*, before a job row exists, for the same
reason ``create_application`` owns the per-posting uniqueness: a surface that
forgets a check must not be able to bypass it. The thread, the portal and the
recruiter console all come through this function.

``submit_application`` is the worker half. It picks the adapter, builds the
context, runs one attempt, and records what happened -- and the status machine
is the only thing that moves the application, so an adapter cannot mark
something submitted that was not.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import storage
from ..config import get_settings
from ..jobs import KIND_APP_SUBMIT, OPEN_STATUSES, Job, enqueue
from ..models import (
    Application,
    ApplicationSubmission,
    Candidate,
    CandidateDocument,
    JobPosting,
    record_event,
    utcnow,
)
from . import alias, limits
from .ats import detect
from .service import ApplicationError, current_documents
from .state import TransitionError, has_consent, transition
from .submit.base import Attachment, SubmissionContext, SubmissionResult
from .submit.registry import get_submitter

logger = logging.getLogger(__name__)


class NotSubmittable(ApplicationError):
    """No adapter can send this one. A Tier-0 hand-off, not a failure."""


def request_submission(
    db: Session,
    application: Application,
    *,
    requested_by: str = "agent",
    refresh_target: bool = True,
) -> Job:
    """Queue one attempt. Raises rather than queueing work that cannot be done.

    Collapses double-clicks the way ``request_package`` does -- an open submit
    job for this application is returned instead of a second one -- because two
    concurrent attempts at the same form is the duplicate-application failure
    the whole module exists to prevent.
    """
    if application.status not in {"prepared", "awaiting_consent"}:
        raise ApplicationError(f"cannot submit an application in {application.status!r}")
    if not has_consent(application):
        raise ApplicationError(
            "no recorded consent: this application needs a per-application yes first"
        )
    decision = limits.may_submit(db, application=application)
    if not decision:
        raise ApplicationError(f"submission budget: {decision.detail}")

    posting = db.get(JobPosting, application.job_posting_id)
    if posting is None:
        raise ApplicationError("application lost its posting")
    # The parse path resolved from the URL alone; this is the moment a follow is
    # worth a request, because somebody is actually trying to apply.
    target = (
        detect.resolve_target(db, posting)
        if refresh_target and posting.ats_resolved_at is None
        else detect.target_of(posting)
    )
    if get_submitter(target) is None:
        raise NotSubmittable(
            f"nothing automated can apply to {posting.company or 'this employer'} "
            f"({target.vendor or 'unrecognised ATS'}); this one is a hand-off"
        )
    if attempts_used(db, application) >= get_settings().apply_max_attempts:
        raise ApplicationError(
            f"already tried {attempts_used(db, application)} times; this one needs a person"
        )

    for job in db.scalars(
        select(Job).where(
            Job.org_id == application.org_id,
            Job.kind == KIND_APP_SUBMIT,
            Job.status.in_(OPEN_STATUSES),
        )
    ).all():
        if (job.payload or {}).get("application_id") == application.id:
            return job

    return enqueue(
        db,
        org_id=application.org_id,
        kind=KIND_APP_SUBMIT,
        idempotency_key=(
            f"{KIND_APP_SUBMIT}:{application.id}:{attempts_used(db, application) + 1}"
        ),
        payload={"application_id": application.id, "requested_by": requested_by},
    )


def first_submission_gate(db: Session, application: Application) -> str:
    """Empty when auto-submit may proceed, otherwise why it may not.

    CANDIDATE_AGENT_PLAN §8: a candidate's *first* submission gets a human look
    before it goes. Not because the second is safer in itself, but because the
    first is the one where a bad resume parse, a wrong name or an answer nobody
    recognises is still systematically wrong for everything that follows.
    """
    prior = db.scalar(
        select(Application).where(
            Application.candidate_id == application.candidate_id,
            Application.id != application.id,
            Application.submitted_at.is_not(None),
        )
    )
    if prior is not None:
        return ""
    return "first application for this candidate: a person reviews it before it goes"


def auto_submit(db: Session, application: Application) -> tuple[Job | None, str]:
    """Queue an attempt if everything allows one. ``(job, reason)``.

    The forgiving entry point, for the surfaces where a submission is a
    side-effect of something else the candidate did (approving a package,
    tapping yes in a thread). It never raises: a hand-off is the normal answer
    for most postings, and a portal tap should not 500 because Taleo exists.
    """
    blocked = first_submission_gate(db, application)
    if blocked:
        return None, blocked
    try:
        return request_submission(db, application, requested_by="candidate"), "queued"
    except NotSubmittable as exc:
        return None, str(exc)
    except ApplicationError as exc:
        return None, str(exc)


def attempts_used(db: Session, application: Application) -> int:
    rows = db.scalars(
        select(ApplicationSubmission).where(ApplicationSubmission.application_id == application.id)
    ).all()
    return len(rows)


def succeeded_submission(db: Session, application: Application) -> ApplicationSubmission | None:
    return db.scalar(
        select(ApplicationSubmission).where(
            ApplicationSubmission.application_id == application.id,
            ApplicationSubmission.status == "succeeded",
        )
    )


def submit_application(db: Session, application: Application) -> dict[str, Any]:
    """Run one attempt. The worker's half; every exit writes an attempt row."""
    already = succeeded_submission(db, application)
    if already is not None:
        # The unique index would catch a genuine race; this catches the ordinary
        # case of a redelivered job and keeps it out of the error path.
        return {"skipped": "already submitted", "submission_id": already.id}

    candidate = db.get(Candidate, application.candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if candidate is None or posting is None:
        raise ApplicationError("application lost its candidate or posting")

    target = detect.target_of(posting) or detect.resolve_target(db, posting)
    submitter = get_submitter(target)
    if submitter is None:
        raise NotSubmittable(f"no submitter for {target.vendor or 'unrecognised ATS'}")

    attempt = ApplicationSubmission(
        org_id=application.org_id,
        application_id=application.id,
        attempt=attempts_used(db, application) + 1,
        vendor=target.vendor,
        method=getattr(submitter, "method", ""),
        status="running",
        started_at=utcnow(),
    )
    db.add(attempt)
    db.flush()

    # Into `submitting` before the adapter runs, so a crashed worker leaves a
    # visible in-flight application rather than one that looks untouched.
    transition(db, application, to_status="submitting", actor="agent")

    ctx = build_context(db, application, candidate=candidate, posting=posting, target=target)
    try:
        result = submitter.submit(ctx)
    except Exception as exc:
        _finish(db, application, attempt, SubmissionResult(ok=False, error=str(exc)[:600]))
        raise

    _finish(db, application, attempt, result)
    return {
        "application_id": application.id,
        "submission_id": attempt.id,
        "vendor": attempt.vendor,
        "method": attempt.method,
        "status": attempt.status,
    }


def _finish(
    db: Session,
    application: Application,
    attempt: ApplicationSubmission,
    result: SubmissionResult,
) -> None:
    attempt.status = result.status
    attempt.external_id = result.external_id[:200]
    attempt.confirmation_ref = result.confirmation_ref[:400]
    attempt.error = (result.error or "")[:600]
    attempt.artifacts = list(result.artifacts)
    attempt.verification = dict(result.verification)
    attempt.finished_at = utcnow() if result.status != "needs_verification" else None
    if result.method:
        attempt.method = result.method
    db.flush()

    record_event(
        db,
        org_id=application.org_id,
        type="application.submission_attempted",
        subject_type="application",
        subject_id=application.id,
        payload={
            "submission_id": attempt.id,
            "vendor": attempt.vendor,
            "method": attempt.method,
            "status": attempt.status,
            "attempt": attempt.attempt,
            "error": attempt.error,
        },
    )

    if result.ok:
        application.submission_method = attempt.method[:32]
        application.submission_url = attempt.confirmation_ref[:400] or application.submission_url
        transition(db, application, to_status="submitted", actor="agent")
        # `transition` only notifies on a recruiter's move -- nobody wants a
        # text announcing what they just did themselves. This is the one case
        # where the agent acted and the candidate genuinely does not know yet.
        from ..messaging.notify import EVENT_APPLICATION_SENT, notify_candidate

        notify_candidate(
            db,
            org_id=application.org_id,
            candidate_id=application.candidate_id,
            event=EVENT_APPLICATION_SENT,
            subject_id=application.id,
        )
        return

    # A parked run stays `submitting`: it is still in flight, waiting on mail to
    # the alias, and showing it as prepared would invite a second attempt.
    if result.needs_verification:
        return

    reason = (
        f"{attempt.vendor or 'submission'} attempt {attempt.attempt} "
        f"{'needs a person' if result.needs_human else 'failed'}: {attempt.error}"
    )
    try:
        transition(db, application, to_status="prepared", actor="agent", reason=reason)
    except TransitionError:
        # Withdrawn under us, or already terminal. The attempt row is the record.
        logger.info("could not park application %s after a failed attempt", application.id)


def resume_verification(db: Session, attempt: ApplicationSubmission) -> dict[str, Any]:
    """Second leg of a parked attempt: the code or link arrived, carry on.

    The same attempt row rather than a new one -- a Workday run that registered
    an account, waited for a verification mail and then submitted is one
    attempt from the employer's point of view, and counting it as two would
    burn the retry budget on our own asynchrony.
    """
    application = db.get(Application, attempt.application_id)
    if application is None:
        return {"skipped": "application not found"}
    candidate = db.get(Candidate, application.candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if candidate is None or posting is None:
        raise ApplicationError("application lost its candidate or posting")

    target = detect.target_of(posting)
    submitter = get_submitter(target)
    resume = getattr(submitter, "resume", None)
    if submitter is None or not callable(resume):
        _finish(
            db,
            application,
            attempt,
            SubmissionResult(
                ok=False,
                needs_human=True,
                error="this submitter cannot resume a parked run",
            ),
        )
        return {"submission_id": attempt.id, "status": attempt.status}

    attempt.status = "running"
    db.flush()
    ctx = build_context(db, application, candidate=candidate, posting=posting, target=target)
    try:
        result = resume(ctx, dict(attempt.verification or {}))
    except Exception as exc:
        _finish(db, application, attempt, SubmissionResult(ok=False, error=str(exc)[:600]))
        raise
    _finish(db, application, attempt, result)
    return {"submission_id": attempt.id, "status": attempt.status}


def build_context(
    db: Session,
    application: Application,
    *,
    candidate: Candidate,
    posting: JobPosting,
    target: detect.ApplyTarget,
) -> SubmissionContext:
    """Everything the adapter is allowed to see, with the bytes already read."""
    documents = current_documents(db, application)
    from_address, reply_to = alias.sending_identity(db, candidate)
    first, _, last = (candidate.full_name or "").strip().partition(" ")

    return SubmissionContext(
        application_id=application.id,
        org_id=application.org_id,
        candidate_id=candidate.id,
        target=target,
        full_name=candidate.full_name or "",
        first_name=first,
        last_name=last.strip(),
        from_address=from_address,
        reply_to_address=reply_to,
        phone=_primary_phone(candidate),
        linkedin_url=candidate.linkedin_url or "",
        location=_location(candidate),
        resume=_attachment(documents.get("resume_generated") or documents.get("resume_uploaded")),
        cover_letter=_attachment(documents.get("cover_letter")),
        answers=list(application.form_answers or []),
        company=posting.company or "",
        title=posting.title or "",
        posting_url=posting.source_url or "",
    )


def _attachment(document: CandidateDocument | None) -> Attachment | None:
    if document is None or not document.storage_path:
        return None
    try:
        content = storage.read_bytes(document.storage_path)
    except OSError as exc:
        logger.warning("document %s is missing from storage: %s", document.id, exc)
        return None
    return Attachment(
        filename=document.filename or f"{document.kind}.pdf",
        content_type=document.content_type or "application/pdf",
        content=content,
    )


def _location(candidate: Candidate) -> str:
    """ "City, Region, Country" from whichever parts we hold."""
    parts = [
        candidate.location_city,
        candidate.location_region,
        candidate.location_country,
    ]
    return ", ".join(part.strip() for part in parts if (part or "").strip())


def _primary_phone(candidate: Candidate) -> str:
    for entry in candidate.phones or []:
        if isinstance(entry, str) and entry.strip():
            return entry.strip()
        if isinstance(entry, dict):
            value = str(entry.get("phone") or entry.get("number") or "").strip()
            if value:
                return value
    return ""

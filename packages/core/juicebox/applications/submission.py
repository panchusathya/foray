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
from datetime import datetime, timedelta
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
from . import alias, credentials, limits
from .ats import detect
from .service import ApplicationError, current_documents
from .state import TransitionError, has_consent, transition
from .submit.base import (
    METHOD_CONSOLE,
    METHOD_MEMORY,
    SECRET_VERIFICATION_KEYS,
    Attachment,
    SubmissionContext,
    SubmissionResult,
)
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
    submitter, _ = submitter_for(db, application, posting, target)
    if submitter is None:
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


def submitter_for(db: Session, application: Application, posting: JobPosting, target):
    """The adapter for this pursuit, with the employer's key if we hold one.

    Both callers need the same two lookups in the same order, and a second copy
    of "which key, which adapter" is how the API path and the form path drift
    apart.
    """
    key = credentials.board_key(
        db, org_id=application.org_id, vendor=target.vendor, tenant=target.tenant
    )
    return get_submitter(target, board_key=key), key


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


def _repair_submitted(db: Session, application: Application) -> None:
    """Move a pursuit whose attempt already succeeded onto `submitted`.

    The adapter ran; the employer has the package. If the status move then
    failed, retries would otherwise skip forever with the row still showing
    `submitting`. Best-effort: a withdrawn application, or one that lost
    consent, stays where it is.
    """
    if application.status in {
        "submitted",
        "acknowledged",
        "interviewing",
        "offer",
        "rejected",
        "withdrawn",
    }:
        return
    try:
        transition(db, application, to_status="submitted", actor="agent")
    except TransitionError:
        logger.exception(
            "application %s has a succeeded attempt but cannot move to submitted",
            application.id,
        )


def submit_application(db: Session, application: Application) -> dict[str, Any]:
    """Run one attempt. The worker's half; every exit writes an attempt row."""
    already = succeeded_submission(db, application)
    if already is not None:
        # The unique index would catch a genuine race; this catches the ordinary
        # case of a redelivered job and keeps it out of the error path. If the
        # attempt row landed but the status move did not (budget, consent, a
        # crash between the two), repair the application so a succeeded send
        # does not sit in `submitting` forever.
        _repair_submitted(db, application)
        return {"skipped": "already submitted", "submission_id": already.id}

    candidate = db.get(Candidate, application.candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if candidate is None or posting is None:
        raise ApplicationError("application lost its candidate or posting")

    target = detect.target_of(posting) or detect.resolve_target(db, posting)
    submitter, board_key = submitter_for(db, application, posting, target)
    if submitter is None:
        raise NotSubmittable(f"no submitter for {target.vendor or 'unrecognised ATS'}")
    if board_key:
        credentials.mark_used(
            db, org_id=application.org_id, vendor=target.vendor, tenant=target.tenant
        )

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
    # visible in-flight application rather than one that looks untouched. Not
    # when we are already there: that is this job being retried after the worker
    # died holding it, and the gates were satisfied on the way in the first time.
    if application.status != "submitting":
        transition(db, application, to_status="submitting", actor="agent")

    ctx = build_context(db, application, candidate=candidate, posting=posting, target=target)
    try:
        result = submitter.submit(ctx)
    except Exception as exc:
        _finish(db, application, attempt, SubmissionResult(ok=False, error=str(exc)[:600]))
        raise

    _remember_account(db, application, target, result)
    _finish(db, application, attempt, result)
    return {
        "application_id": application.id,
        "submission_id": attempt.id,
        "vendor": attempt.vendor,
        "method": attempt.method,
        "status": attempt.status,
    }


def _remember_account(db: Session, application: Application, target, result) -> None:
    """Keep the portal login a browser run just created.

    Workday and Oracle make you register before you can apply, so without this
    the second role at the same employer registers again -- another account,
    another verification mail, and two candidate records at one company. The
    adapter reports the credential on its result; storing it is the service's
    job, because an adapter must not reach the database.
    """
    payload = result.verification or {}
    if not payload.get("username") or not payload.get("password"):
        return
    from .submit.browser.workday import remember_account

    try:
        remember_account(
            db,
            org_id=application.org_id,
            candidate_id=application.candidate_id,
            vendor=target.vendor,
            tenant=target.tenant,
            username=str(payload["username"]),
            password=str(payload["password"]),
        )
    except Exception as exc:  # noqa: BLE001 - never lose an attempt over this
        logger.warning("could not store the portal account for %s: %s", application.id, exc)


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
    attempt.verification = _seal(result.verification)
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
        try:
            transition(db, application, to_status="submitted", actor="agent")
        except TransitionError:
            # The employer already has it. Rolling the attempt back would lie
            # about a send we cannot un-send; a later redelivery repairs via
            # `_repair_submitted`.
            logger.exception(
                "application %s attempt %s succeeded but could not move to submitted",
                application.id,
                attempt.id,
            )
            return
        _notify_sent(db, application, attempt)
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


def _notify_sent(db: Session, application: Application, attempt: ApplicationSubmission) -> None:
    """Tell the candidate their application went out -- when it actually did.

    `transition` only notifies on a recruiter's move, because nobody wants a
    text announcing what they just did themselves. A successful attempt is the
    one case where the agent acted and the candidate genuinely does not know.

    Except in a dry run. `ConsoleSubmitter` and `MemorySubmitter` return
    ok=True on purpose, so that a rehearsal exercises the status machine and
    the attempt row without an employer hearing anything. The notification is
    the one part of that rehearsal that reaches a real person, and "your
    application has been sent" is false when nothing was sent. A dry run must
    be invisible outside our own records.
    """
    if attempt.method in {METHOD_CONSOLE, METHOD_MEMORY}:
        logger.info(
            "not notifying candidate %s: attempt %s was a %s dry run, nothing was sent",
            application.candidate_id,
            attempt.id,
            attempt.method,
        )
        return

    from ..messaging.notify import EVENT_APPLICATION_SENT, notify_candidate

    notify_candidate(
        db,
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        event=EVENT_APPLICATION_SENT,
        subject_id=application.id,
    )
    _email_sent_confirmation(db, application)


def _email_sent_confirmation(db: Session, application: Application) -> None:
    """The same news as the text, from the address that carries the application.

    Two channels on purpose. The text is the fast one; this is the one that is
    searchable a month later, and it arrives from the alias mailbox, so the
    confirmation and every employer reply that follows sit in one thread in the
    candidate's own inbox.

    Never raises: a confirmation that could not be sent must not undo a
    submission that already reached the employer.
    """
    candidate = db.get(Candidate, application.candidate_id)
    if candidate is None:
        return
    real = alias.primary_email(candidate)
    if not real:
        return
    posting = db.get(JobPosting, application.job_posting_id)
    title = (posting.title if posting else "") or "the role"
    company = (posting.company if posting else "") or "the company"
    alias_address = alias.address_for_application(application.id)

    try:
        from ..outreach.sender import OutboundEmail, sender_for_org
        from ..portal.links import mint_portal_link

        link = mint_portal_link(
            db,
            org_id=application.org_id,
            candidate_id=candidate.id,
            path=f"/applications/{application.id}",
            channel="email",
        )
        lines = [
            f"Your application for {title} at {company} is in.",
            "",
            f"It went out from {alias_address}, so replies from {company} arrive there"
            " and I'll pass them straight on to you.",
            "",
            f"What I sent, and where it is: {link}",
        ]
        result = sender_for_org(db, application.org_id).send(
            OutboundEmail(
                to=real,
                subject=f"Applied: {title} at {company}",
                text_body="\n".join(lines),
                html_body="",
                from_email=alias_address or "",
                from_name="Foray",
            )
        )
        if not result.ok:
            logger.warning(
                "could not email the sent-confirmation for %s: %s", application.id, result.error
            )
    except Exception as exc:  # noqa: BLE001 - never undo a real submission over this
        logger.warning("could not email the sent-confirmation for %s: %s", application.id, exc)


def _seal(verification: dict) -> dict:
    """Encrypt the credential keys before the attempt row is written.

    Everything else in ``verification`` -- the run log, the stage, the tenant --
    stays readable, because that is what makes a parked attempt diagnosable.
    Only the secrets move behind ``integrations/crypto``, the same treatment
    mailbox OAuth tokens and ``ats_accounts.token_blob`` already get.
    """
    payload = dict(verification or {})
    secrets_held = {key: payload.pop(key) for key in SECRET_VERIFICATION_KEYS if key in payload}
    if secrets_held:
        from ..integrations.crypto import encrypt_json

        payload["secret_blob"] = encrypt_json(secrets_held)
    return payload


def _unseal(verification: dict | None) -> dict:
    """The adapter's view: secrets put back, blob removed.

    A blob that no longer decrypts (SECRET_KEY rotated under a parked run) is
    dropped rather than raised on -- the adapter then reports that it has no
    credential and the attempt ends as ``needs_human``, which is the honest
    outcome and one a person can act on.
    """
    payload = dict(verification or {})
    blob = payload.pop("secret_blob", "")
    if not blob:
        return payload
    from ..integrations.crypto import decrypt_json

    try:
        payload.update(decrypt_json(str(blob)))
    except ValueError as exc:
        logger.warning("a parked submission's credential could not be read: %s", exc)
    return payload


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
        result = resume(ctx, _unseal(attempt.verification))
    except Exception as exc:
        _finish(db, application, attempt, SubmissionResult(ok=False, error=str(exc)[:600]))
        raise
    _finish(db, application, attempt, result)
    return {"submission_id": attempt.id, "status": attempt.status}


def expire_parked(db: Session, *, org_id: str, now: datetime | None = None) -> int:
    """Age out attempts waiting on mail that never came.

    A verification email that does not arrive -- filtered, bounced, a tenant
    that verifies some other way -- leaves an application in ``submitting``
    forever, which reads to everyone as "still working on it". After the window
    it becomes what it actually is: something needing a person.
    """
    now = now or utcnow()
    cutoff = now - timedelta(hours=max(get_settings().apply_verification_hours, 1))
    stale = db.scalars(
        select(ApplicationSubmission).where(
            ApplicationSubmission.org_id == org_id,
            ApplicationSubmission.status == "needs_verification",
            ApplicationSubmission.started_at < cutoff,
        )
    ).all()
    for attempt in stale:
        application = db.get(Application, attempt.application_id)
        if application is None:
            continue
        _finish(
            db,
            application,
            attempt,
            SubmissionResult(
                ok=False,
                method=attempt.method,
                needs_human=True,
                error=(
                    "no verification mail arrived within "
                    f"{get_settings().apply_verification_hours}h"
                ),
            ),
        )
    if stale:
        logger.info("aged out %d parked submission(s) for %s", len(stale), org_id)
    return len(stale)


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
    from_address, reply_to = alias.sending_identity(db, candidate, application)
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

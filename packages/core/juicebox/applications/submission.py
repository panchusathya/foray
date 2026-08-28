"""Sending an application: the one door in, and the worker half.

``request_submission`` is the only way an automated attempt starts. The
submission budget is checked *here*, before a job row exists, for the same
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
from .state import TransitionError, transition
from .submit.base import (
    METHOD_CONSOLE,
    METHOD_MEMORY,
    REDACTED,
    SECRET_VERIFICATION_KEYS,
    Attachment,
    SubmissionContext,
    SubmissionResult,
    SubmitDeferred,
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
    if not target:
        target = _form_target(posting)
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


def first_submission_gate(_db: Session, _application: Application) -> str:
    """Compatibility hook for future product-level submission gates.

    JuiceBox treats use of the product as authorization to submit, so there is
    no separate per-application approval gate here.
    """
    return ""


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
    # Only missing work authorization withholds a send — that is the one
    # fact we will not guess, and the one question chat still asks. Other
    # required blanks are filled from the package or become a hand-off
    # inside the submitter, not another round of survey questions.
    from .followups import required_form_blanks

    waiting = required_form_blanks(application)
    if waiting:
        named = "; ".join(
            str(item.get("question") or item.get("key")) for item in waiting[:3]
        )
        return None, f"waiting on required form answers: {named}"

    # Account-gated boards wait for a candidate-chosen vault credential before
    # any attempt (model B2): the agent registers with the candidate's
    # password, never one it minted behind an unreadable email loop. Prompt
    # the vault once and hold; the vault save re-queues this.
    posting = db.get(JobPosting, application.job_posting_id)
    target = detect.target_of(posting) if posting is not None else None
    if target and candidate_credential_needed(db, application, target):
        prompt_vault_credential(db, application, target)
        return None, f"waiting on a vault credential for {target.vendor}"

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


def _form_target(posting: JobPosting) -> detect.ApplyTarget:
    """A target for an unrecognised board: no vendor, just where the form is.

    ``target_of`` answers empty for an unrecognised board, and the browser
    agent still needs ``ctx.target.apply_url`` to navigate. Deliberately
    vendorless -- inventing a vendor here would leak a guess into every row
    that stores ``target.vendor`` as a fact.
    """
    return detect.ApplyTarget(
        vendor="",
        apply_url=(posting.apply_url or posting.source_url or "").strip(),
        tier=detect.TIER_BROWSER,
    )


def attempts_used(db: Session, application: Application) -> int:
    """How many attempts this pursuit has spent, against ``apply_max_attempts``.

    Every row counts, ``running`` included: a row exists only once the adapter
    is genuinely being run (see ``submit_application``), so a crash mid-run is
    an attempt the employer's form may well have seen.
    """
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
    `submitting`. Best-effort: a withdrawn application stays where it is.
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


def _over_the_attempt_cap(db: Session, application: Application) -> dict[str, Any] | None:
    """``None`` when another attempt is allowed, otherwise the hand-off.

    Returned rather than raised: raising would send the job round the queue's
    backoff to be refused identically four more times, and the answer is not
    going to change. The pursuit goes back to `prepared` with the reason on it,
    which is what a person picking this up needs to read.
    """
    used = attempts_used(db, application)
    if used < get_settings().apply_max_attempts:
        return None
    reason = f"already tried {used} times; this one needs a person"
    if application.status == "submitting":
        try:
            transition(db, application, to_status="prepared", actor="agent", reason=reason)
        except TransitionError:
            logger.info("could not park application %s at the attempt cap", application.id)
    return {"skipped": reason, "application_id": application.id}


def submit_application(db: Session, application: Application) -> dict[str, Any]:
    """Run one attempt. The worker's half.

    Every exit *from the adapter onwards* writes an attempt row. The exits
    before it -- already submitted, over the retry cap, refused entry to
    `submitting` -- deliberately do not: a row that never reached an adapter is
    a retry the candidate is charged for and no employer ever saw.
    """
    already = succeeded_submission(db, application)
    if already is not None:
        # The unique index would catch a genuine race; this catches the ordinary
        # case of a redelivered job and keeps it out of the error path. If the
        # attempt row landed but the status move did not (budget, a
        # crash between the two), repair the application so a succeeded send
        # does not sit in `submitting` forever.
        _repair_submitted(db, application)
        return {"skipped": "already submitted", "submission_id": already.id}

    # The cap belongs here as well as in `request_submission`, because the queue
    # is the other way in and it retries to WORKER_MAX_ATTEMPTS -- a larger,
    # unrelated number. Without this the real ceiling on browser sessions
    # filling an employer's form is the worker's, not the one a deployment set.
    capped = _over_the_attempt_cap(db, application)
    if capped:
        return capped

    candidate = db.get(Candidate, application.candidate_id)
    posting = db.get(JobPosting, application.job_posting_id)
    if candidate is None or posting is None:
        raise ApplicationError("application lost its candidate or posting")

    target = detect.target_of(posting) or detect.resolve_target(db, posting)
    if not target:
        target = _form_target(posting)
    submitter, board_key = submitter_for(db, application, posting, target)
    if submitter is None:
        raise NotSubmittable(f"no submitter for {target.vendor or 'unrecognised ATS'}")
    if board_key:
        credentials.mark_used(
            db, org_id=application.org_id, vendor=target.vendor, tenant=target.tenant
        )

    # Into `submitting` before the adapter runs, so a crashed worker leaves a
    # visible in-flight application rather than one that looks untouched. Not
    # when we are already there: that is this job being retried after the worker
    # died holding it, and the gates were satisfied on the way in the first time.
    #
    # And *before* the attempt row, not after. This move can refuse -- the
    # candidate hit the weekly cap while the job sat in the queue -- and an
    # attempt row written ahead of it counts against `attempts_used` for a try
    # no adapter ever made. Five refused retries used to spend the whole retry
    # budget and lock the application out with "this one needs a person",
    # leaving five rows stuck at `running` that nothing sweeps.
    if application.status != "submitting":
        transition(db, application, to_status="submitting", actor="agent")

    recovered = _recover_checkpointed(db, application, candidate, posting, target, submitter)
    if recovered is not None:
        return recovered

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

    ctx = build_context(db, application, candidate=candidate, posting=posting, target=target)
    try:
        profile = _browser_profile_for(db, application, submitter, target)
    except Exception as exc:
        from .submit.browser.cloud import ProfileQuota

        if isinstance(exc, ProfileQuota):
            _finish(
                db,
                application,
                attempt,
                SubmissionResult(ok=False, method="browser", needs_human=True, error=str(exc)[:600]),
            )
            return {
                "application_id": application.id,
                "submission_id": attempt.id,
                "vendor": attempt.vendor,
                "method": attempt.method,
                "status": attempt.status,
            }
        raise
    ctx.browser_profile = profile
    ctx.persist_checkpoint = _checkpoint_writer(db, attempt)
    try:
        result = submitter.submit(ctx)
    except SubmitDeferred:
        db.delete(attempt)
        db.flush()
        if application.status == "submitting":
            try:
                transition(
                    db,
                    application,
                    to_status="prepared",
                    actor="agent",
                    reason="browser capacity; will retry",
                )
            except TransitionError:
                logger.info("could not park application %s after a deferred submit", application.id)
        raise
    except Exception as exc:
        _finish(db, application, attempt, SubmissionResult(ok=False, error=str(exc)[:600]))
        raise

    if profile is not None:
        from .submit.browser.profiles import persist as persist_profile

        persist_profile(db, profile, user_agent=get_settings().apply_browser_user_agent)
    _remember_account(db, application, target, result)
    _finish(db, application, attempt, result)
    return {
        "application_id": application.id,
        "submission_id": attempt.id,
        "vendor": attempt.vendor,
        "method": attempt.method,
        "status": attempt.status,
    }


def _checkpoint_writer(db: Session, attempt: ApplicationSubmission):
    """Persist planner state onto the running attempt before an irreversible click."""

    def persist(payload: dict) -> None:
        held = dict(attempt.verification or {})
        held["run"] = payload
        attempt.verification = held
        db.flush()

    return persist


def _recover_checkpointed(db, application, candidate, posting, target, submitter) -> dict | None:
    """If a prior run requested final_submit, inspect that session. Never re-click."""
    running = db.scalar(
        select(ApplicationSubmission).where(
            ApplicationSubmission.application_id == application.id,
            ApplicationSubmission.status == "running",
        )
    )
    if running is None:
        return None
    payload = _unseal(running.verification)
    run = payload.get("run") or {}
    if not run.get("final_submit_requested_at") or not run.get("provider_session_id"):
        return None
    resume = getattr(submitter, "resume", None)
    if not callable(resume):
        return None
    ctx = build_context(db, application, candidate=candidate, posting=posting, target=target)
    ctx.persist_checkpoint = _checkpoint_writer(db, running)
    result = resume(ctx, payload)
    _finish(db, application, running, result)
    return {
        "application_id": application.id,
        "submission_id": running.id,
        "vendor": running.vendor,
        "method": running.method,
        "status": running.status,
        "recovered": True,
    }


def _browser_profile_for(db: Session, application: Application, submitter, target):
    """The candidate's warm profile for this run, when profiles are on.

    ``None`` in every other case, and ``None`` is always safe: the driver
    falls back to the proven cold context. The public-form agent loads a
    Browser Use cloud profile scoped to this origin. Workday/YC keep the
    local encrypted storage_state path and never receive a cloud profile --
    blending a Workday login into a public-form jar is the failure the
    origin split exists to prevent.
    """
    settings = get_settings()
    if getattr(submitter, "method", "") != "browser":
        return None
    from urllib.parse import urlparse

    from .submit.browser.cloud import agent_configured
    from .submit.browser.profiles import cloud_profile_for, origin_key, profile_for
    from .submit.registry import TWO_LEG_VENDORS

    apply_url = target.apply_url or ""
    if agent_configured() and target.vendor not in TWO_LEG_VENDORS:
        origin = origin_key(apply_url)
        return cloud_profile_for(
            db,
            org_id=application.org_id,
            candidate_id=application.candidate_id,
            origin=origin,
        )
    # A static CDP endpoint (legacy scraping browser, or a test fake) owns the
    # fingerprint; overlaying a local storage_state is the tell. Cloud profiles
    # for the agent path are loaded above, before this gate.
    if getattr(settings, "apply_browser_cdp_url", ""):
        return None
    if not settings.apply_browser_profiles:
        return None
    host = (urlparse(apply_url).netloc or "").split(":")[0].lower()
    return profile_for(
        db,
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        run_hosts={host},
    )


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
            tenant=_account_tenant(target),
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
    # A verdict that carries no evidence does not destroy the evidence already
    # on the row. `expire_parked` and a refused resume both arrive here with
    # nothing but a method and an error, and blanking a parked Workday attempt
    # would take with it the screenshot, the run log, the tenant and the sealed
    # password for a portal account that still exists on the employer's side --
    # leaving a needs_human ticket with nothing in it for a person to act on.
    attempt.status = result.status
    attempt.external_id = result.external_id[:200] or attempt.external_id
    attempt.confirmation_ref = result.confirmation_ref[:400] or attempt.confirmation_ref
    attempt.error = (result.error or "")[:600]
    if result.artifacts:
        attempt.artifacts = list(result.artifacts)
    if result.verification:
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


#: A key whose *name* says it holds a credential, wherever it sits. The
#: top-level keys are sealed and handed back on resume; a nested copy -- the
#: browser run log used to carry ``run.filled.password`` verbatim -- is
#: redacted instead, because nothing reads a credential back out of a
#: diagnostic record and a value nobody needs must not survive at all.
_SECRET_NAME_MARKERS = ("password", "secret", "credential", "token")


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_NAME_MARKERS)


def _redact(value):
    """Every nested credential-shaped value replaced with the marker."""
    if isinstance(value, dict):
        return {
            # Our own sealed blob is the one thing named like a secret that is
            # already safe, and redacting it would throw away the credential
            # leg two needs.
            key: item
            if key == "secret_blob"
            else (REDACTED if _looks_secret(str(key)) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _seal(verification: dict) -> dict:
    """Encrypt the credential keys before the attempt row is written.

    Everything else in ``verification`` -- the run log, the stage, the tenant --
    stays readable, because that is what makes a parked attempt diagnosable.
    Only the secrets move behind ``integrations/crypto``, the same treatment
    mailbox OAuth tokens and ``ats_accounts.token_blob`` already get.

    Popping the top level is not enough on its own: an adapter that nests a
    credential (``verification["run"]["filled"]["password"]``, which is exactly
    what the Workday account form produced) would write it to the row in clear,
    and on the branches with no top-level copy there would be nothing to
    encrypt at all. So the whole payload is swept.
    """
    payload = dict(verification or {})
    secrets_held = {key: payload.pop(key) for key in SECRET_VERIFICATION_KEYS if key in payload}
    payload = _redact(payload)
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
    portal_username, portal_password, portal_verified = _portal_account(db, candidate, target)

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
        current_title=candidate.current_title or "",
        current_company=candidate.current_company or "",
        portal_username=portal_username,
        portal_password=portal_password,
        portal_account_verified=portal_verified,
        resume=_attachment(documents.get("resume_generated") or documents.get("resume_uploaded")),
        cover_letter=_attachment(documents.get("cover_letter")),
        answers=list(application.form_answers or []),
        company=posting.company or "",
        title=posting.title or "",
        posting_url=posting.source_url or "",
    )


#: Vendors that make a candidate register before they can apply, and are
#: therefore the only ones a stored portal login means anything for.
_REGISTERING_VENDORS = (detect.VENDOR_WORKDAY, detect.VENDOR_ORACLE, detect.VENDOR_YC)


def _account_tenant(target) -> str:
    """The namespace a stored portal login is keyed on.

    Workday and Oracle accounts belong to one employer's tenant. A YC account
    does not: the login lives at workatastartup.com and spans every YC
    company, while ``target.tenant`` carries the company slug -- keying on it
    would register a fresh account per employer and park each one on a
    verification mail that reads as a duplicate signup.
    """
    if target.vendor == detect.VENDOR_YC:
        from .submit.browser.yc import ACCOUNT_TENANT

        return ACCOUNT_TENANT
    return target.tenant


def _portal_account(db: Session, candidate: Candidate, target) -> tuple[str, str, bool]:
    """``(username, password, verified)`` for this tenant's stored login.

    Read here rather than in the adapter for the reason ``_remember_account``
    writes here: the seam is a plain value object, and an adapter holding a
    ``Session`` is one refactor away from moving the pursuit itself.
    ``verified`` tells the adapter whether to sign in (True) or register with
    the stored password (False -- a candidate-picked vault credential).
    """
    if target.vendor not in _REGISTERING_VENDORS:
        return "", "", False
    from .candidate_credentials import account_state
    from .submit.browser.workday import stored_account

    try:
        username = stored_account(
            db, candidate_id=candidate.id, vendor=target.vendor, tenant=_account_tenant(target)
        )[0]
        password, verified = account_state(
            db, candidate_id=candidate.id, vendor=target.vendor, tenant=_account_tenant(target)
        )
        return username, password, verified
    except Exception as exc:  # noqa: BLE001 - a missing login is registering again
        logger.warning("could not read the stored portal account: %s", exc)
        return "", "", False


def candidate_credential_needed(db: Session, application: Application, target) -> bool:
    """Whether an account-gated board is waiting on a candidate-chosen login.

    Model B2 (operator decision, 2026-08-27): the candidate picks the password
    in the vault and the agent registers the account with it, rather than the
    agent minting one and self-registering behind an email-verification loop we
    cannot reliably read. A fresh account-gated pursuit therefore waits for
    that credential instead of proceeding.
    """
    from .submit.registry import ACCOUNT_GATED_VENDORS

    if target.vendor not in ACCOUNT_GATED_VENDORS:
        return False
    from .candidate_credentials import account_state

    password, _verified = account_state(
        db, candidate_id=application.candidate_id, vendor=target.vendor, tenant=_account_tenant(target)
    )
    return not password


def prompt_vault_credential(db: Session, application: Application, target) -> None:
    """Ask the candidate, once, to set a vault password for this employer."""
    from ..messaging.notify import EVENT_VAULT_NEEDED, notify_candidate

    notify_candidate(
        db,
        org_id=application.org_id,
        candidate_id=application.candidate_id,
        event=EVENT_VAULT_NEEDED,
        subject_id=application.id,
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

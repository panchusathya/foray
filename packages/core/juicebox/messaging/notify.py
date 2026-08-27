"""Proactive sends: the deliberate carve-out from "never two outbound in a row".

A notification is a message nobody asked for, which is exactly why every gate
in the stack applies to it and none can be skipped:

* ``cadence.may_initiate`` — quiet hours (deferred to the window via
  ``Job.run_at``, no scheduler) and the per-org opt-out circuit breaker.
  Deliberately NOT the nudge budget: that counts nudges, and a notification's
  own dedup is the event-keyed job. When it did apply here, two early
  notifications spent the budget and every later application-sent text on
  the thread was silently skipped.
* ``suppression.is_blocked`` and the consent check in ``_deliver``.
* The proposal budgets, when the notification carries cards.

Bodies are **canned templates, never model-written**: there is nothing
inbound to anchor a model, and a hallucinated claim texted unprompted is the
worst place for one. The documented outbound carve-outs are now three — the
greeting, the STOP acknowledgement, and these.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..intake.schema import coverage
from ..jobs import KIND_THREAD_NOTIFY, Job, enqueue
from ..models import Application, IntakeMessage, IntakeThread, JobPosting
from ..portal.links import mint_portal_link
from . import cadence, prompts
from .channels import OutboundMessage
from .manager import MANAGER_MODES, present_matches
from .suppression import is_blocked
from .threads import INBOUND, STATUS_ACTIVE

logger = logging.getLogger(__name__)

EVENT_PROFILE_READY = "profile.ready"
EVENT_MATCHES_READY = "matches.ready"
EVENT_PACKAGE_READY = "package.ready"
EVENT_APPLICATION_SENT = "application.sent"
EVENT_STATUS_CHANGED = "status.changed"
EVENT_REFERRAL_CONFIRMED = "referral.confirmed"
# Apply-by-link dead ends, subject-keyed on the link message so a retried
# job cannot text the same refusal twice.
EVENT_LINK_PROBLEM = "link.problem"
EVENT_LINK_DUPLICATE = "link.duplicate"
EVENT_LINK_PARKED = "link.parked"

_LINK_EVENT_BODIES = {
    EVENT_LINK_PROBLEM: prompts.NOTIFY_LINK_PROBLEM,
    EVENT_LINK_DUPLICATE: prompts.NOTIFY_LINK_DUPLICATE,
    EVENT_LINK_PARKED: prompts.NOTIFY_LINK_PARKED,
}


def manager_thread_for(db: Session, *, org_id: str, candidate_id: str) -> IntakeThread | None:
    """The candidate's standing line to the agent, if they have one."""
    if not candidate_id:
        return None
    return db.scalar(
        select(IntakeThread)
        .where(
            IntakeThread.org_id == org_id,
            IntakeThread.candidate_id == candidate_id,
            IntakeThread.status == STATUS_ACTIVE,
            IntakeThread.mode.in_(tuple(MANAGER_MODES)),
        )
        .order_by(IntakeThread.last_inbound_at.desc(), IntakeThread.created_at.desc())
        .limit(1)
    )


def enqueue_notify(
    db: Session, *, thread: IntakeThread, event: str, subject_id: str = ""
) -> Job:
    """Event-keyed: a rescore firing twice cannot double-text anybody."""
    return enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_THREAD_NOTIFY,
        idempotency_key=f"{KIND_THREAD_NOTIFY}:{thread.id}:{event}:{subject_id}",
        payload={"thread_id": thread.id, "event": event, "subject_id": subject_id},
    )


def handle_notify(db: Session, job: Job, now) -> dict[str, Any]:
    from .conversation import _deliver

    thread = db.get(IntakeThread, job.payload.get("thread_id") or "")
    event = str(job.payload.get("event") or "")
    if thread is None:
        return {"skipped": "thread not found"}
    if thread.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if thread.mode not in MANAGER_MODES:
        return {"skipped": f"mode {thread.mode} takes no notifications"}
    if thread.status != STATUS_ACTIVE or is_blocked(db, thread=thread):
        return {"skipped": "thread inactive or blocked"}

    decision = cadence.may_initiate(db, thread=thread, now=now, kind=cadence.INITIATIVE_NOTIFY)
    if not decision.allowed:
        if decision.reason == cadence.REASON_QUIET_HOURS and decision.resume_at:
            return {"defer": True, "defer_until": decision.resume_at, "reason": decision.reason}
        # Budgets and the circuit breaker: waiting does not restore them, and
        # the event that mattered will fire again if it still matters.
        return {"skipped": decision.reason}

    if event in (EVENT_MATCHES_READY, EVENT_PROFILE_READY):
        # The best profile.ready notification IS the first cards. When none
        # clear the bar yet, profile.ready gets its canned line and
        # matches.ready stays silent -- unprompted "nothing new" is noise.
        result = present_matches(
            db, thread=thread, state=coverage(thread.record or {}), origin="proactive",
            quiet=True,
        )
        if result is not None:
            return {"event": event, "action": result.action, "sent": len(result.sent)}
        if event == EVENT_PROFILE_READY:
            row = _deliver(
                db,
                thread=thread,
                message=OutboundMessage(body=prompts.NOTIFY_PROFILE_READY),
                origin="proactive",
            )
            return {"event": event, "sent": 1 if row is not None else 0}
        return {"event": event, "skipped": "nothing clears the bar"}

    if event in (EVENT_PACKAGE_READY, EVENT_APPLICATION_SENT):
        application = db.get(Application, job.payload.get("subject_id") or "")
        if application is None or application.candidate_id != thread.candidate_id:
            return {"skipped": "application not found for this thread"}
        posting = db.get(JobPosting, application.job_posting_id)
        link = mint_portal_link(
            db,
            org_id=thread.org_id,
            candidate_id=application.candidate_id,
            path=f"/applications/{application.id}",
            channel=thread.channel,
        )
        template = (
            prompts.NOTIFY_APPLICATION_SENT
            if event == EVENT_APPLICATION_SENT
            else prompts.NOTIFY_PACKAGE_READY
        )
        # clean_title: a scraped headline that already ends "at Wipro Limited"
        # must not render "... at Wipro Limited at Wipro Limited" here.
        from ..presentation.jobcard import clean_title

        body = template.format(
            title=clean_title(posting.title, posting.company) if posting else "the role",
            company=posting.company if posting else "the company",
            link=link,
        )
        # verbatim: names the company and role from the posting.
        media = ()
        if event == EVENT_APPLICATION_SENT:
            media = _sent_screenshot(db, thread, application)
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=body, media=media, verbatim=True),
            origin="proactive",
        )
        reacted = False
        if event == EVENT_APPLICATION_SENT and application.consenting_message_id:
            from .conversation import react_to_message

            consenting = db.get(IntakeMessage, application.consenting_message_id)
            # Tapback-apply binds consent to the card that was tapped, which
            # is our outbound — 👍 that bubble would look like we liked our
            # own card. Only react when the yes was a candidate inbound
            # ("apply to 2").
            if (
                consenting is not None
                and consenting.thread_id == thread.id
                and consenting.direction == INBOUND
            ):
                reacted = react_to_message(
                    db, thread=thread, message=consenting, reaction="like"
                )
        result = {"event": event, "sent": 1 if row is not None else 0}
        if event == EVENT_APPLICATION_SENT:
            result["reacted"] = reacted
        if event == EVENT_PACKAGE_READY and row is not None:
            # The follow-up questions ride *behind* the ready message, here,
            # because ordering them anywhere else does not order them: the
            # worker used to queue this notification and then ask inline, so
            # the questions always overtook the message they were about --
            # "what should we put for comp?" arriving before "your
            # application is ready" reads as two different products.
            # ask_for_missing is idempotent per application (asked flags), so
            # a retried notify job does not re-ask.
            from ..applications.followups import ask_for_missing

            result["followup"] = ask_for_missing(db, application)
        return result

    if event in _LINK_EVENT_BODIES:
        row = _deliver(
            db,
            thread=thread,
            message=OutboundMessage(body=_LINK_EVENT_BODIES[event]),
            origin="proactive",
        )
        return {"event": event, "sent": 1 if row is not None else 0}

    if event == EVENT_STATUS_CHANGED:
        from ..applications.service import application_status_summary

        summary = application_status_summary(
            db, org_id=thread.org_id, candidate_id=thread.candidate_id or ""
        )
        body = prompts.NOTIFY_STATUS_CHANGED.format(summary=summary)
        row = _deliver(db, thread=thread, message=OutboundMessage(body=body), origin="proactive")
        return {"event": event, "sent": 1 if row is not None else 0}

    if event == EVENT_REFERRAL_CONFIRMED:
        from ..models import Referral

        referral = db.get(Referral, job.payload.get("subject_id") or "")
        if referral is None or referral.org_id != thread.org_id:
            return {"skipped": "referral not found for this thread"}
        # The bonus comes off the row, not out of settings. The referrer was
        # promised a number in the group chat; this is that same number even
        # if the configured reward has moved since.
        body = prompts.NOTIFY_REFERRAL_CONFIRMED.format(
            bonus=referral.bonus_applications
        )
        row = _deliver(db, thread=thread, message=OutboundMessage(body=body), origin="proactive")
        return {"event": event, "sent": 1 if row is not None else 0}

    return {"skipped": f"unknown event {event!r}"}


def _sent_screenshot(db: Session, thread, application) -> tuple:
    """Confirmation PNG for a succeeded browser submit, or empty.

    Same media contract as job cards: iMessage/RCS only, body always kept,
    any failure stays text-only. The path is the one labelled ``confirmation``
    on the succeeded attempt — never a before-submit or unconfirmed shot.
    """
    from .. import storage
    from ..applications.submit.browser.agent.loop import confirmation_artifact
    from ..models import ApplicationSubmission
    from .cards import wants_image
    from .linq import LinqSendError, OutboundMedia, upload_attachment

    if not wants_image(thread):
        return ()
    attempt = db.scalar(
        select(ApplicationSubmission).where(
            ApplicationSubmission.application_id == application.id,
            ApplicationSubmission.status == "succeeded",
        )
    )
    if attempt is None:
        return ()
    artifacts = list(attempt.artifacts or [])
    run = (attempt.verification or {}).get("run") or {}
    artifacts.extend(run.get("artifacts") or [])
    relpath = confirmation_artifact(artifacts)
    if not relpath:
        return ()
    try:
        data = storage.read_bytes(relpath)
    except Exception as exc:  # noqa: BLE001 - text still goes
        logger.info("confirmation screenshot unreadable for %s: %s", application.id, exc)
        return ()
    if not data:
        return ()
    try:
        attachment_id = upload_attachment(
            filename="application-sent.png",
            content_type="image/png",
            data=data,
        )
    except LinqSendError as exc:
        logger.info("confirmation screenshot upload refused for %s: %s", application.id, exc)
        return ()
    except Exception as exc:  # noqa: BLE001
        logger.info("confirmation screenshot upload failed for %s: %s", application.id, exc)
        return ()
    if not attachment_id:
        return ()
    return (
        OutboundMedia(
            content_type="image/png",
            attachment_id=attachment_id,
            filename="application-sent.png",
        ),
    )


def notify_candidate(
    db: Session, *, org_id: str, candidate_id: str, event: str, subject_id: str = ""
) -> Job | None:
    """The one-call emitter: queue a notification if the candidate has a line.

    ``utcnow`` never appears in the key -- callers pass a subject that makes
    the event's own identity the idempotency (an application id, a date
    bucket), so retries collapse and genuinely new events do not.
    """
    thread = manager_thread_for(db, org_id=org_id, candidate_id=candidate_id)
    if thread is None:
        return None
    return enqueue_notify(db, thread=thread, event=event, subject_id=subject_id)

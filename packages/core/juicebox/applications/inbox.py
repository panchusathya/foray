"""Mail that arrives at a candidate's application alias.

This is the other half of owning the identity. An employer's ATS writes to
``anna+<token>@goforay.io``, we receive it, and a handful of different things
can be in the envelope:

1. **A verification link.** The portal wants a click before the account is
   real. We click it and un-park the submission that was waiting.
2. **A one-time code.** Same shape, different mechanism: park the code on the
   attempt and let the browser run continue.
3. **A status message.** A received-your-application acknowledgement, or a
   rejection. Those move the pursuit.
4. **An interview or assessment invitation.** Labelled, so the tracker can
   filter on it, but deliberately *not* status-moving -- see ``classify``.
5. **Anything else** -- a recruiter writing to a person. That is relayed and
   nothing is inferred from it.

Every one of them is stored as an ``ApplicationMessage`` before it is relayed.
The relay hands the mail to the candidate's own inbox and forgets it; the row
is what the portal tracker reads back.

**It never reaches the recruiter-reply machinery.** ``integrations/sync.py``
routes alias mail here and returns, because everything downstream of that --
intent classification, reply drafting, enrollment stopping -- is built for a
recruiter's outbound getting a reply from a candidate, and an ATS
acknowledgement is neither.

**Classification is deliberately timid.** A rejection moves an application to a
terminal status, and a false positive there tells somebody they were turned
down when they were not. So: an explicit phrase, matched against an application
we can actually identify from the sender, or nothing happens and a human reads
the relayed mail.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..jobs import KIND_APP_SUBMIT_VERIFY, enqueue
from ..models import (
    Application,
    ApplicationMessage,
    ApplicationSubmission,
    Candidate,
    JobPosting,
    record_event,
    utcnow,
)
from ..outreach.sender import OutboundEmail, sender_for_org
from . import alias
from .state import TransitionError, transition

logger = logging.getLogger(__name__)

KIND_VERIFICATION = "verification"
KIND_OTP = "otp"
KIND_ACKNOWLEDGED = "acknowledged"
KIND_REJECTED = "rejected"
KIND_INTERVIEW = "interview"
KIND_ASSESSMENT = "assessment"
KIND_OTHER = "other"

#: The filters the tracker offers, in the order it shows them.
TRACKER_KINDS = (
    KIND_VERIFICATION,
    KIND_REJECTED,
    KIND_INTERVIEW,
    KIND_ASSESSMENT,
)

#: A link whose text or URL says it verifies something. Kept narrow: clicking
#: an arbitrary link in mail addressed to us is how an alias becomes a
#: server-side request forgery.
_VERIFY_LINK = re.compile(
    r"https://[^\s\"'<>]*(?:verif|activat|confirm|validate)[^\s\"'<>]*",
    re.IGNORECASE,
)
#: A code the copy actually calls a code. A bare six-digit number in an email
#: is as likely to be a requisition id.
_OTP = re.compile(
    r"(?:one[- ]time|verification|security|access|confirmation|login)\s*"
    r"(?:pass)?code\D{0,20}(\d{4,8})",
    re.IGNORECASE,
)
_OTP_TRAILING = re.compile(r"\bcode\s*(?:is|:)\s*(\d{4,8})\b", re.IGNORECASE)

_ACK_PHRASES = (
    "we have received your application",
    "we've received your application",
    "thank you for applying",
    "thanks for applying",
    "your application has been received",
    "application received",
    "we received your submission",
)
_INTERVIEW_PHRASES = (
    "schedule an interview",
    "schedule a call",
    "invite you to interview",
    "invitation to interview",
    "like to set up a time",
    "would like to speak with you",
    "book a time",
    "phone screen",
    "next round",
)
_ASSESSMENT_PHRASES = (
    "take-home",
    "take home assignment",
    "coding challenge",
    "online assessment",
    "complete the assessment",
    "skills test",
    "hackerrank",
    "codesignal",
    "karat",
)
_REJECT_PHRASES = (
    "we will not be moving forward",
    "we won't be moving forward",
    "not moving forward with your application",
    "decided not to move forward",
    "we have decided to pursue other candidates",
    "pursue other candidates",
    "unfortunately, we will not",
    "your application was not selected",
    "no longer under consideration",
)


@dataclass(frozen=True)
class InboundClassification:
    kind: str
    #: The verification URL, or the OTP digits, depending on ``kind``.
    detail: str = ""
    phrase: str = ""


def classify(subject: str, body: str) -> InboundClassification:
    """What this message is, from its own words. No model, no network."""
    blob = f"{subject or ''}\n{body or ''}"
    lowered = blob.lower()

    code = _OTP.search(blob) or _OTP_TRAILING.search(blob)
    if code:
        return InboundClassification(KIND_OTP, detail=code.group(1))
    link = _VERIFY_LINK.search(blob)
    if link:
        return InboundClassification(KIND_VERIFICATION, detail=link.group(0).rstrip(").,>\"'"))
    # Rejection before acknowledgement: a rejection letter routinely opens by
    # thanking you for applying, and reading that as an acknowledgement would
    # park somebody at "received" forever.
    for phrase in _REJECT_PHRASES:
        if phrase in lowered:
            return InboundClassification(KIND_REJECTED, phrase=phrase)
    # Interview and assessment also come wrapped in "thank you for applying",
    # and both are the news the candidate is actually waiting for, so they win
    # over the acknowledgement too. Neither moves the application on its own --
    # they are labels on a message, and a status the agent infers from a phrase
    # is exactly the false positive `classify` is written to avoid.
    for phrase in _INTERVIEW_PHRASES:
        if phrase in lowered:
            return InboundClassification(KIND_INTERVIEW, phrase=phrase)
    for phrase in _ASSESSMENT_PHRASES:
        if phrase in lowered:
            return InboundClassification(KIND_ASSESSMENT, phrase=phrase)
    for phrase in _ACK_PHRASES:
        if phrase in lowered:
            return InboundClassification(KIND_ACKNOWLEDGED, phrase=phrase)
    return InboundClassification(KIND_OTHER)


def _bare(address: str) -> str:
    """The address out of a ``Name <addr>`` header value."""
    _, bare = parseaddr((address or "").strip())
    return bare or (address or "").strip()


#: Local parts that exist to refuse mail. Matched on the local part only --
#: "noreply.co" is a real domain and "no-reply@" on it is still a robot.
_NO_REPLY_LOCALS = ("no-reply", "noreply", "donotreply", "do-not-reply", "no_reply")


def repliable_address(message: ApplicationMessage) -> str:
    """Where a reply to this message should go, or empty if nowhere.

    ``Reply-To`` first: ATS mail is sent *from* a robot and puts the address
    that routes back -- a recruiter, or the vendor's own threading address --
    in Reply-To. Falling back to ``From`` catches the humans who write
    directly.

    Empty is a real answer, not a failure. Workday verification mail has no
    recipient behind it, and offering a reply box that bounces is worse than
    saying so.
    """
    for candidate_address in (message.reply_to_address, message.from_address):
        bare = _bare(candidate_address).lower()
        if not bare or "@" not in bare:
            continue
        local = bare.partition("@")[0]
        if any(marker in local for marker in _NO_REPLY_LOCALS):
            continue
        return bare
    return ""


def handle(
    db: Session,
    *,
    org_id: str,
    to_addresses: tuple[str, ...] | list[str],
    from_address: str,
    subject: str,
    body: str,
    native_id: str = "",
    received_at: datetime | None = None,
    reply_to: str = "",
    rfc_message_id: str = "",
) -> dict:
    """Act on one message addressed to an alias. Returns what it decided.

    Called from ``integrations/sync`` with the provider's own event already
    normalised, so this function is pure application logic and testable without
    a mailbox.
    """
    candidate = None
    matched_address = ""
    alias_application_id = ""
    for address in to_addresses:
        candidate, alias_application_id = alias.resolve(db, address)
        if candidate is not None:
            matched_address = address
            break
    if candidate is None:
        return {"routed": False, "reason": "not one of our aliases"}
    if candidate.org_id != org_id:
        # The mailbox is shared; an alias belongs to exactly one org.
        return {"routed": False, "reason": "org mismatch"}

    # A sync page is replayed whenever the worker dies holding one, and the
    # ingest is only free if it is idempotent. Without this, a replay clicks a
    # verification link a second time and re-moves a status that a human may
    # already have corrected. Still `routed`: it was our alias, and the caller
    # must not fall through to the recruiter-reply machinery.
    if native_id:
        seen = db.scalar(
            select(ApplicationMessage.id).where(
                ApplicationMessage.org_id == org_id,
                ApplicationMessage.provider_native_id == native_id,
            )
        )
        if seen is not None:
            return {"routed": True, "reason": "already handled", "candidate_id": candidate.id}

    classification = classify(subject, body)
    # An application-scoped alias already answers this exactly. The domain
    # heuristic below is only for legacy candidate-scoped aliases, where the
    # address says who but not which pursuit.
    application = db.get(Application, alias_application_id) if alias_application_id else None
    if application is None:
        application = _match_application(db, candidate, from_address=from_address)
    outcome: dict = {
        "routed": True,
        "candidate_id": candidate.id,
        "alias": matched_address,
        "kind": classification.kind,
        "application_id": application.id if application is not None else "",
    }

    if classification.kind == KIND_VERIFICATION:
        outcome.update(_verify(db, candidate, classification.detail))
    elif classification.kind == KIND_OTP:
        outcome.update(_park_code(db, candidate, classification.detail))
    elif classification.kind in {KIND_ACKNOWLEDGED, KIND_REJECTED} and application is not None:
        outcome.update(
            _move(db, application, to_status=classification.kind, phrase=classification.phrase)
        )

    record_event(
        db,
        org_id=org_id,
        type="application.inbound_mail",
        subject_type="candidate",
        subject_id=candidate.id,
        payload={
            "kind": classification.kind,
            "from": from_address,
            "subject": (subject or "")[:200],
            "application_id": outcome.get("application_id", ""),
        },
    )
    # The message itself, kept. The event above records that something arrived;
    # this is what lets the tracker show what was said.
    display_name, bare_from = parseaddr(from_address or "")
    stored = ApplicationMessage(
        org_id=org_id,
        candidate_id=candidate.id,
        application_id=application.id if application is not None else None,
        kind=classification.kind,
        alias_address=matched_address[:320],
        from_address=(bare_from or from_address or "")[:320],
        from_name=(display_name or "")[:200],
        reply_to_address=_bare(reply_to)[:320],
        subject=(subject or "")[:500],
        body=body or "",
        provider_native_id=(native_id or "")[:200],
        rfc_message_id=(rfc_message_id or "")[:400],
        received_at=received_at or utcnow(),
    )
    db.add(stored)
    db.flush()
    outcome["message_id"] = stored.id

    outcome["relayed"] = relay(
        db,
        candidate=candidate,
        alias_address=matched_address,
        from_address=from_address,
        subject=subject,
        body=body,
    )
    stored.relayed = bool(outcome["relayed"])
    db.flush()
    return outcome


def _match_application(
    db: Session, candidate: Candidate, *, from_address: str
) -> Application | None:
    """Which pursuit this message is about, or ``None`` if we cannot tell.

    Matched on the sender's domain against the employer, because that is the
    only signal in an ATS email we can trust -- the subject line is the
    requisition title if we are lucky. When it does not match, we do not guess:
    a wrong match moves the wrong application to rejected.
    """
    domain = (from_address or "").rpartition("@")[2].strip().lower()
    if not domain:
        return None
    rows = db.scalars(
        select(Application)
        .where(
            Application.candidate_id == candidate.id,
            Application.status.in_(("submitting", "submitted", "acknowledged", "interviewing")),
        )
        .order_by(Application.updated_at.desc())
    ).all()
    for application in rows:
        posting = db.get(JobPosting, application.job_posting_id)
        if posting is None:
            continue
        haystack = " ".join(
            part.lower()
            for part in (
                posting.company_domain,
                posting.company_normalized,
                posting.company,
                posting.ats_tenant,
                posting.apply_url,
            )
            if part
        )
        stem = domain.split(".")[0]
        if domain in haystack or (len(stem) > 3 and stem in haystack):
            return application
    # Exactly one open pursuit is not ambiguous, whoever the sender is.
    return rows[0] if len(rows) == 1 else None


def _parked(db: Session, candidate: Candidate) -> ApplicationSubmission | None:
    """The attempt waiting on mail, if there is one."""
    return db.scalar(
        select(ApplicationSubmission)
        .join(Application, Application.id == ApplicationSubmission.application_id)
        .where(
            Application.candidate_id == candidate.id,
            ApplicationSubmission.status == "needs_verification",
        )
        .order_by(ApplicationSubmission.created_at.desc())
    )


def _verify(db: Session, candidate: Candidate, url: str) -> dict:
    """Click a verification link, then un-park whatever was waiting on it."""
    attempt = _parked(db, candidate)
    if attempt is None:
        return {"action": "ignored", "reason": "no parked submission to verify"}

    from .submit.browser.driver import BrowserUnavailable, open_verification_link

    try:
        clicked = open_verification_link(url)
    except BrowserUnavailable as exc:
        logger.warning("cannot click a verification link on this worker: %s", exc)
        return {"action": "blocked", "reason": str(exc)}
    if not clicked:
        return {"action": "failed", "reason": "verification link did not open"}

    # Merge into the stored payload without touching `secret_blob`: these two
    # keys are not secrets, and re-sealing here would mean decrypting a
    # credential this function has no use for.
    state = dict(attempt.verification or {})
    state["verified_at"] = utcnow().isoformat()
    state["verification_url"] = url
    attempt.verification = state
    db.flush()
    job = enqueue(
        db,
        org_id=attempt.org_id,
        kind=KIND_APP_SUBMIT_VERIFY,
        idempotency_key=f"{KIND_APP_SUBMIT_VERIFY}:{attempt.id}:verified",
        payload={"submission_id": attempt.id},
    )
    return {"action": "verified", "submission_id": attempt.id, "job_id": job.id}


def _park_code(db: Session, candidate: Candidate, code: str) -> dict:
    attempt = _parked(db, candidate)
    if attempt is None:
        return {"action": "ignored", "reason": "no parked submission wants a code"}
    state = dict(attempt.verification or {})
    state["otp"] = code
    state["otp_at"] = utcnow().isoformat()
    attempt.verification = state
    db.flush()
    job = enqueue(
        db,
        org_id=attempt.org_id,
        kind=KIND_APP_SUBMIT_VERIFY,
        idempotency_key=f"{KIND_APP_SUBMIT_VERIFY}:{attempt.id}:otp:{code}",
        payload={"submission_id": attempt.id},
    )
    return {"action": "code_parked", "submission_id": attempt.id, "job_id": job.id}


def _move(db: Session, application: Application, *, to_status: str, phrase: str) -> dict:
    try:
        transition(
            db,
            application,
            to_status=to_status,
            actor="agent",
            reason=f"employer mail: {phrase}"[:240],
        )
    except TransitionError as exc:
        return {"action": "not_moved", "reason": str(exc)}
    return {"action": f"moved_to_{to_status}"}


def relay(
    db: Session,
    *,
    candidate: Candidate,
    alias_address: str,
    from_address: str,
    subject: str,
    body: str,
) -> bool:
    """Forward employer mail to the candidate's own inbox.

    ``From`` is the alias, never the employer: sending as ``no-reply@acme.com``
    from our infrastructure fails SPF, fails DKIM alignment and gets binned by
    the recipient's DMARC. Every masked-mail service rewrites the From for the
    same reason. That rewrite is the compliant behaviour, not a shortcut.

    ``Reply-To`` is the alias as well, which is the part worth arguing about.
    Pointing it at the employer would be the obvious choice and is wrong twice
    over: Zoho refuses to send an unverified Reply-To at all, and a reply that
    went straight to the employer would come *from* the candidate's own address
    -- one the employer has no record of, on an application they filed under
    ours. Pointing it back at the alias means a reply lands somewhere we can
    attribute, whatever client it was sent from.

    The footer says all of this out loud, because a rewritten From with no
    explanation reads as spam.
    """
    if not get_settings().apply_alias_relay:
        return False
    real = alias.primary_email(candidate)
    if not real or real == alias_address:
        return False

    sender_line = _bare(from_address)
    footer = "\n".join(
        [
            "",
            "—" * 24,
            "Reply to this email and we will send it on as you — the employer",
            "sees the same address your application came from.",
            "",
            "Replying from any other address sends from one they have no record of.",
            "",
            f"This is your Foray managed address. Forwarding to: {real}",
            f"Sent from: {sender_line or 'an address the employer did not disclose'}",
        ]
    )
    result = sender_for_org(db, candidate.org_id).send(
        OutboundEmail(
            to=real,
            subject=subject or "(no subject)",
            text_body=f"{body}\n{footer}\n",
            html_body="",
            from_email=alias_address,
            from_name=f"{candidate.full_name or 'Applications'} via Foray",
            headers={"Reply-To": alias_address} if alias_address else {},
        )
    )
    if not result.ok:
        logger.warning("could not relay employer mail to %s: %s", real, result.error)
    return result.ok

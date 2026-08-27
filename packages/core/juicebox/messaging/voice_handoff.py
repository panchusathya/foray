"""The live voice-call handoff: verify the number before sending job cards.

After a candidate hears a preview and explicitly asks for details by text,
``start_text_verification`` sends one canned Go4A message while they are still
on the call. It is deliberately the only outbound-first caller of
``linq.start_chat``. The tool event and recorded call are the consent evidence;
it must never become a generic cold-outreach seam.

The first real non-STOP reply proves control of that chat. Only then does
``confirm_text_verification`` enqueue the card-delivery job. The job waits for
post-call extraction and the rescore, then delivers through that exact thread.
This direct call-to-thread link prevents a fast reply from racing identity
linkage, and makes retries idempotent. Capability failures, suppression, and a
new STOP park the promise for a recruiter rather than sending a fallback.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..intake.service import STATUS_EXTRACTED, TEXT_PROMISE_REASON, counterparty_number
from ..jobs import KIND_INTAKE_MATCH_TEXT, Job, enqueue
from ..models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    IntakeThread,
    JobMatchWatermark,
    record_event,
    utcnow,
)
from ..outreach.suppressions import is_suppressed
from . import consent
from .channels import LINQ, DeliveryResult, LinqChannel, OutboundMessage
from .linq import LinqSendError, check_imessage, send_message, start_chat
from .manager import MODE_MANAGING
from .notify import EVENT_MATCHES_READY, enqueue_notify
from .prompts import VOICE_VERIFICATION_GREETING
from .threads import find_active_thread, open_thread, record_outbound, stopped_thread_exists

logger = logging.getLogger(__name__)

# A STOP that arrived after the call outranks the call's consent. Different
# reason string on purpose: this call must NOT be followed up by text.
OPTED_OUT_REASON = "match list promised by text, but the number has since opted out -- do not text"

# How long the handler waits for the post-call rescore before sending with
# whatever the book holds. The rescore is queued in the same transaction as
# the preference write and normally lands in seconds; past this, a thin list
# beats an unkept promise.
FRESHNESS_PATIENCE = timedelta(minutes=30)

MODE_VOICE_VERIFY = "voice_verify"

VERIFICATION_GUIDANCE = (
    "The verification text was sent. Ask them to reply with 'hey' or 'hello' "
    "when it arrives, then thank them and end the call. Do not read or send "
    "the role details until they reply by text."
)


def schedule_match_text(db: Session, call: IntakeCall) -> Job:
    """Queue the follow-up. Idempotent on the call — one promise, one text."""
    return enqueue(
        db,
        org_id=call.org_id,
        kind=KIND_INTAKE_MATCH_TEXT,
        idempotency_key=f"{KIND_INTAKE_MATCH_TEXT}:{call.id}",
        payload={"intake_call_id": call.id},
    )


def start_text_verification(db: Session, call: IntakeCall) -> dict[str, Any]:
    """Send the live-call verification text after an explicit request.

    This sends only the number-control prompt. A genuine inbound message is
    required before the job cards may be delivered.
    """
    if not (call.matches_offered or []):
        return {"sent": False, "guidance": "No roles were presented. Close normally."}
    if call.text_verification_status in {"sent", "confirmed"}:
        return {"sent": True, "guidance": VERIFICATION_GUIDANCE}

    key = lookup_key(counterparty_number(call))
    settings = get_settings()
    if not key or not settings.linq_enabled or not LinqChannel().is_configured():
        return _verification_unavailable(db, call)
    if is_suppressed(db, org_id=call.org_id, phone=key) or stopped_thread_exists(
        db, channel=LINQ, from_address=key
    ):
        return _verification_unavailable(db, call, reason=OPTED_OUT_REASON)

    greeting = _greeting(db, call)
    thread = find_active_thread(db, channel=LINQ, from_address=key)
    try:
        if thread is not None:
            if thread.candidate_id and call.candidate_id and thread.candidate_id != call.candidate_id:
                return _verification_unavailable(
                    db,
                    call,
                    reason="an active text thread on this number belongs to someone else -- follow up manually",
                )
            if not thread.provider_thread_id:
                return _verification_unavailable(db, call)
            message_id = send_message(
                chat_id=thread.provider_thread_id,
                body=greeting,
                idempotency_key=f"voice-verify:{call.id}",
            )
        else:
            if not check_imessage(key):
                return _verification_unavailable(db, call)
            chat_id, message_id = start_chat(
                to_handle=key,
                body=greeting,
                idempotency_key=f"voice-verify:{call.id}",
            )
            thread = open_thread(
                db,
                org_id=call.org_id,
                channel=LINQ,
                provider="linq",
                provider_thread_id=chat_id,
                entry_point="voice_handoff",
                candidate_id=call.candidate_id,
                from_address=key,
                to_address=lookup_key(settings.linq_from_number) or settings.linq_from_number,
                mode=MODE_VOICE_VERIFY,
            )
    except LinqSendError as exc:
        logger.warning("voice verification send failed for call %s: %s", call.id, exc)
        return _verification_unavailable(db, call)

    call.text_verification_thread_id = thread.id
    call.text_verification_status = "sent"
    call.text_verification_sent_at = utcnow()
    # The agent may call this only after the candidate explicitly asks for the
    # role details by text; the tool event and transcript are the evidence.
    call.sms_opt_in = True
    consent.record_consent(
        db,
        org_id=call.org_id,
        channel="voice",
        scope=consent.SCOPE_APPLICATION_UPDATES,
        source=consent.SOURCE_VOICE_WRAP_UP,
        disclosure_text=consent.DISCLOSURE_VOICE_SMS,
        disclosure_version=consent.DISCLOSURE_VOICE_SMS_VERSION,
        candidate_id=call.candidate_id,
        call_id=call.id,
    )
    record_outbound(
        db,
        thread=thread,
        message=OutboundMessage(body=greeting, verbatim=True),
        result=DeliveryResult(status="sent", provider_native_id=message_id),
        origin="proactive",
    )
    record_event(
        db,
        org_id=call.org_id,
        type="intake.text_verification.sent",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"thread_id": thread.id, "message_id": message_id},
    )
    return {"sent": True, "guidance": VERIFICATION_GUIDANCE}


def confirm_text_verification(db: Session, *, thread: IntakeThread) -> dict[str, Any] | None:
    """Use the first actual reply as the trigger for delivering job cards."""
    call = db.scalar(
        select(IntakeCall)
        .where(
            IntakeCall.org_id == thread.org_id,
            IntakeCall.text_verification_thread_id == thread.id,
            IntakeCall.text_verification_status == "sent",
        )
        .order_by(IntakeCall.text_verification_sent_at.desc())
        .limit(1)
    )
    if call is None:
        return None

    call.text_verification_status = "confirmed"
    call.text_verification_confirmed_at = utcnow()
    thread.consent_messaging = True
    thread.mode = MODE_MANAGING
    if call.candidate_id and not thread.candidate_id:
        thread.candidate_id = call.candidate_id
    schedule_match_text(db, call)
    record_event(
        db,
        org_id=call.org_id,
        type="intake.text_verification.confirmed",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"thread_id": thread.id},
    )
    return {"call_id": call.id, "thread_id": thread.id}


def handle_match_text(db: Session, job: Job, now) -> dict[str, Any]:
    call = db.get(IntakeCall, str((job.payload or {}).get("intake_call_id") or ""))
    if call is None:
        return {"skipped": "call not found"}
    if call.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if call.text_verification_status != "confirmed":
        return {"skipped": "text number not confirmed"}
    if not (call.matches_offered or []):
        return {"skipped": "nothing was pitched"}
    if call.status != STATUS_EXTRACTED:
        return {"defer": True}
    if not call.candidate_id:
        return _park(
            db,
            call,
            "verified the number, but the call is unattributed -- "
            "attach it to a person, then send manually",
        )

    thread = db.get(IntakeThread, call.text_verification_thread_id or "")
    if thread is None:
        return _park(db, call, TEXT_PROMISE_REASON)
    key = lookup_key(counterparty_number(call))
    if is_suppressed(db, org_id=call.org_id, phone=key) or stopped_thread_exists(
        db, channel=LINQ, from_address=key
    ):
        return _park(db, call, OPTED_OUT_REASON)

    # Wait for the post-call rescore, but only after the number has been
    # proved. The user never gets a role card from a call they did not answer.
    if not _matches_fresh(db, call) and not _patience_exhausted(call, now):
        return {"defer": True}
    if thread.candidate_id and thread.candidate_id != call.candidate_id:
        return _park(db, call, "the confirmed text thread belongs to someone else -- follow up manually")
    thread.candidate_id = call.candidate_id
    thread.mode = MODE_MANAGING
    enqueue_notify(db, thread=thread, event=EVENT_MATCHES_READY, subject_id=call.id)
    record_event(
        db,
        org_id=call.org_id,
        type="intake.match_text.queued",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"thread_id": thread.id, "via": "verified_reply"},
    )
    return {"delivered_via": "verified_reply", "thread_id": thread.id}


def can_text_by_linq(db: Session, *, org_id: str, phone: str) -> bool:
    """Whether the handoff could plausibly text this number, checked mid-call.

    Cheap checks only — configuration, suppression, stopped threads. The
    iMessage capability check is a network call and stays post-call; a promise
    that then parks is caught by the review queue, and the guidance's "if they
    say no" line already names the recruiter fallback.
    """
    settings = get_settings()
    if not settings.linq_enabled or not LinqChannel().is_configured():
        return False
    key = lookup_key(phone)
    if not key:
        return False
    if is_suppressed(db, org_id=org_id, phone=key):
        return False
    return not stopped_thread_exists(db, channel=LINQ, from_address=key)


def _greeting(db: Session, call: IntakeCall) -> str:
    candidate = db.get(Candidate, call.candidate_id) if call.candidate_id else None
    first = (candidate.first_name or "").strip() if candidate is not None else ""
    name = f" {first}" if first else ""
    return VOICE_VERIFICATION_GREETING.format(
        name=name, recruiter=get_settings().intake_recruiter_name
    )


def _verification_unavailable(
    db: Session, call: IntakeCall, *, reason: str = TEXT_PROMISE_REASON
) -> dict[str, Any]:
    """Tell the live agent to close honestly and leave a recruiter handoff."""
    _park(db, call, reason)
    return {
        "sent": False,
        "guidance": "The verification text could not be sent. Tell them a recruiter will follow up, then end the call.",
    }


def _park(db: Session, call: IntakeCall, reason: str) -> dict[str, Any]:
    call.review_needed = True
    existing = (call.review_reason or "").strip()
    if reason not in existing:
        call.review_reason = "; ".join(p for p in (existing, reason) if p)[:400]
    record_event(
        db,
        org_id=call.org_id,
        type="intake.match_text.parked",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"candidate_id": call.candidate_id, "reason": reason},
    )
    return {"parked": reason}


def _matches_fresh(db: Session, call: IntakeCall) -> bool:
    """Has the post-call rescore landed? No preference row means yes.

    Same comparison the match tick makes, in the other direction: the
    watermark's clock at or past the preference's clock means the book
    reflects this call.
    """
    preference = db.scalar(
        select(CandidatePreference).where(
            CandidatePreference.candidate_id == call.candidate_id,
            CandidatePreference.org_id == call.org_id,
        )
    )
    if preference is None or preference.updated_at is None:
        return True
    mark = db.scalar(
        select(JobMatchWatermark).where(
            JobMatchWatermark.org_id == call.org_id,
            JobMatchWatermark.candidate_id == call.candidate_id,
        )
    )
    if mark is None or mark.computed_at is None:
        return False
    return _aware(mark.computed_at) >= _aware(preference.updated_at)


def _patience_exhausted(call: IntakeCall, now) -> bool:
    anchor = call.ended_at or call.created_at
    if anchor is None:
        return True
    return _aware(now) - _aware(anchor) > FRESHNESS_PATIENCE


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)

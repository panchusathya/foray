"""The voice call's text follow-up: keep the promise the wrap-up pitch made.

A finished intake call ends with roles pitched and — after an explicit,
recorded opt-in — the offer to text the full list. This module is what makes
that text actually arrive. It is deliberately the *only* place in the codebase
that originates a chat nobody texted first (``linq.start_chat``), because it
is the only outbound-first send with a consent story behind it: the candidate
rang us (or asked to be rung), heard the roles, and said yes to texts on a
recorded line, with the flag latched on the call row and the transcript as the
evidence record. This is not cold outreach and must never become the seam one
grows through — a new caller of ``start_chat`` needs its own consent story.

Routing, in order:

1. **A standing managing thread wins.** The list rides the existing
   ``matches.ready`` notification into the conversation they already have —
   keyed on the call id, not the daily bucket, so a promise made on this call
   delivers even if a routine rescore already texted them today.
2. **Otherwise Linq opens a new chat**, iMessage-capable handles only (the
   rollout decision in ``SMS_ENABLEMENT.md``), gated on ``LINQ_ENABLED``, the
   suppression list, and stopped threads. One canned greeting, then the match
   cards through the ordinary notify machinery with every one of its gates.
3. **Anything that cannot send parks on the review queue.** A promise made on
   a recorded call lands on a human rather than evaporating — and a STOP that
   arrived after the call is honoured over the call's consent, because the
   latest signal wins.

The job defers until the candidate's rescore has landed: the cards read
``JobMatch`` rows a queued worker writes, and a greeting followed by silence
is worse than a greeting a minute later.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..intake.phones import lookup_key
from ..intake.service import TEXT_PROMISE_REASON, counterparty_number
from ..jobs import KIND_INTAKE_MATCH_TEXT, Job, enqueue
from ..models import (
    Candidate,
    CandidatePreference,
    IntakeCall,
    JobMatchWatermark,
    record_event,
)
from ..outreach.suppressions import is_suppressed
from . import consent
from .channels import LINQ, DeliveryResult, LinqChannel, OutboundMessage
from .linq import LinqSendError, check_imessage, start_chat
from .manager import MODE_MANAGING
from .notify import EVENT_MATCHES_READY, enqueue_notify, manager_thread_for
from .prompts import VOICE_HANDOFF_GREETING
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


def schedule_match_text(db: Session, call: IntakeCall) -> Job:
    """Queue the follow-up. Idempotent on the call — one promise, one text."""
    return enqueue(
        db,
        org_id=call.org_id,
        kind=KIND_INTAKE_MATCH_TEXT,
        idempotency_key=f"{KIND_INTAKE_MATCH_TEXT}:{call.id}",
        payload={"intake_call_id": call.id},
    )


def handle_match_text(db: Session, job: Job, now) -> dict[str, Any]:
    call = db.get(IntakeCall, str((job.payload or {}).get("intake_call_id") or ""))
    if call is None:
        return {"skipped": "call not found"}
    if call.org_id != job.org_id:
        return {"skipped": "org_mismatch"}
    if not call.sms_opt_in:
        return {"skipped": "no opt-in"}
    if not (call.matches_offered or []):
        return {"skipped": "nothing was pitched"}
    if not call.candidate_id:
        return _park(
            db,
            call,
            "opted in for the match list, but the call is unattributed -- "
            "attach it to a person, then send manually",
        )

    thread = manager_thread_for(db, org_id=call.org_id, candidate_id=call.candidate_id)
    if thread is not None:
        enqueue_notify(db, thread=thread, event=EVENT_MATCHES_READY, subject_id=call.id)
        record_event(
            db,
            org_id=call.org_id,
            type="intake.match_text.queued",
            subject_type="intake_call",
            subject_id=call.id,
            payload={"via": "existing_thread", "thread_id": thread.id},
        )
        return {"delivered_via": "existing_thread", "thread_id": thread.id}

    # The cards read JobMatch rows a queued rescore writes. Wait for it —
    # briefly. A greeting followed by "nothing clears the bar" silence is the
    # failure this defer exists to prevent.
    if not _matches_fresh(db, call) and not _patience_exhausted(call, now):
        return {"defer": True}

    key = lookup_key(counterparty_number(call))
    if not key:
        return _park(db, call, TEXT_PROMISE_REASON)
    settings = get_settings()
    if not settings.linq_enabled or not LinqChannel().is_configured():
        return _park(db, call, TEXT_PROMISE_REASON)
    if is_suppressed(db, org_id=call.org_id, phone=key) or stopped_thread_exists(
        db, channel=LINQ, from_address=key
    ):
        return _park(db, call, OPTED_OUT_REASON)

    # A thread outside manager modes, or one a died retry already created.
    # Never a second start_chat for a number that has an open conversation.
    existing = find_active_thread(db, channel=LINQ, from_address=key)
    if existing is not None:
        if existing.candidate_id != call.candidate_id:
            return _park(
                db,
                call,
                "an active text thread on this number belongs to someone else -- "
                "resolve the identity, then send manually",
            )
        # Their text intake, if one was mid-flight, has nothing left to ask —
        # the voice call closed the gate — so the thread graduates to managing
        # and the cards ride the ordinary notification.
        existing.mode = MODE_MANAGING
        enqueue_notify(db, thread=existing, event=EVENT_MATCHES_READY, subject_id=call.id)
        record_event(
            db,
            org_id=call.org_id,
            type="intake.match_text.queued",
            subject_type="intake_call",
            subject_id=call.id,
            payload={"via": "existing_linq_thread", "thread_id": existing.id},
        )
        return {"delivered_via": "existing_linq_thread", "thread_id": existing.id}

    if not check_imessage(key):
        return _park(db, call, TEXT_PROMISE_REASON)

    greeting = _greeting(db, call)
    try:
        chat_id, message_id = start_chat(
            to_handle=key, body=greeting, idempotency_key=f"{KIND_INTAKE_MATCH_TEXT}:{call.id}"
        )
    except LinqSendError as exc:
        logger.warning("voice handoff chat create failed for call %s: %s", call.id, exc)
        return _park(db, call, TEXT_PROMISE_REASON)

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
        consent_messaging=True,
        contact_name=call.contact_name or "",
        contact_email=call.contact_email or "",
        # Managing from the first message: the intake happened on the phone,
        # so there is no onboarding flow to run here.
        mode=MODE_MANAGING,
    )
    # The thread's consent provenance is the voice wrap-up, not an inbound
    # text — the disclosure recorded is the one this person actually heard.
    consent.record_consent(
        db,
        org_id=call.org_id,
        channel=LINQ,
        scope=consent.SCOPE_APPLICATION_UPDATES,
        source=consent.SOURCE_VOICE_WRAP_UP,
        disclosure_text=consent.DISCLOSURE_VOICE_SMS,
        disclosure_version=consent.DISCLOSURE_VOICE_SMS_VERSION,
        candidate_id=call.candidate_id,
        thread_id=thread.id,
    )
    record_outbound(
        db,
        thread=thread,
        # verbatim: carries the recruiter's and possibly the caller's name.
        message=OutboundMessage(body=greeting, verbatim=True),
        result=DeliveryResult(status="sent", provider_native_id=message_id),
        origin="proactive",
    )
    enqueue_notify(db, thread=thread, event=EVENT_MATCHES_READY, subject_id=call.id)
    record_event(
        db,
        org_id=call.org_id,
        type="intake.match_text.sent",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"thread_id": thread.id, "chat_id": chat_id},
    )
    return {"delivered_via": "linq", "thread_id": thread.id}


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
    return VOICE_HANDOFF_GREETING.format(
        name=name, recruiter=get_settings().intake_recruiter_name
    )


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

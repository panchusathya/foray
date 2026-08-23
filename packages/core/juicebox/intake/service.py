"""Intake domain logic: who owns this call, and what came out of it.

Deliberately not in ``intake/__init__.py``. ``repository`` imports
``intake.phones``, so anything the package exports on import must not reach back
into ``repository`` — and this module is one import away from wanting to.

The webhook handler and the worker both call in here, so neither owns any of it.
The handler's whole job is to verify a signature, resolve an org and return 200
fast; Retell times out at 10 seconds and retries three times, and an extraction
that runs inline turns one slow model call into four duplicate conversations.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..jobs import KIND_INTAKE_EXTRACT, enqueue
from ..models import (
    Candidate,
    CandidatePhone,
    CandidatePreference,
    IntakeCall,
    IntakeNumber,
    record_event,
    utcnow,
)
from .extract import ExtractionResult, extract_intake, known_context
from .providers import EVENT_ANALYZED, EVENT_ENDED, EVENT_STARTED, CallEvent
from .schema import SCHEMA_VERSION, coverage

logger = logging.getLogger(__name__)

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_EXTRACTED = "extracted"
STATUS_FAILED = "failed"

# How many questions the mid-call check hands back. One at a time keeps the
# conversation a conversation; a list of six becomes an interrogation, and the
# model will try to ask them all in one breath.
COVERAGE_QUESTION_LIMIT = 2


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------


def resolve_org_id(db: Session, event: CallEvent, *, provider: str) -> str | None:
    """Which org owns this call. Never reads the payload for an org id.

    Two trusted sources, in order:

    1. A row we already wrote for this call id. Covers every retry, and covers
       web calls — we placed those ourselves, so the org was known before the
       provider ever called back.
    2. The number that was dialled. The caller does not choose it.

    ``None`` means refuse the request. Guessing an org here is a cross-tenant
    write with a phone bill attached.
    """
    existing = find_call(db, provider=provider, provider_call_id=event.provider_call_id)
    if existing is not None:
        return existing.org_id

    number = event.our_number
    if not number:
        return None
    row = db.scalar(
        select(IntakeNumber).where(IntakeNumber.phone == number, IntakeNumber.active.is_(True))
    )
    return row.org_id if row is not None else None


def find_call(db: Session, *, provider: str, provider_call_id: str) -> IntakeCall | None:
    if not provider_call_id:
        return None
    return db.scalar(
        select(IntakeCall).where(
            IntakeCall.provider == provider,
            IntakeCall.provider_call_id == provider_call_id,
        )
    )


def resolve_candidate_id(db: Session, *, org_id: str, phone: str) -> str | None:
    """Caller identification by number, scoped to the org.

    Authorises *accepting* an update. It does not authorise reading the record
    back to whoever dialled — ANI is spoofable, so disclosure needs the caller
    to confirm something only they would know. That gate lives in the agent
    prompt (Phase 4), not here, but it is the reason this returns an id and not
    a profile.
    """
    if not phone:
        return None
    row = db.scalar(
        select(CandidatePhone).where(
            CandidatePhone.org_id == org_id, CandidatePhone.phone == phone
        )
    )
    return row.candidate_id if row is not None else None


# --------------------------------------------------------------------------
# recording events
# --------------------------------------------------------------------------


def record_call_event(
    db: Session,
    *,
    org_id: str,
    provider: str,
    event: CallEvent,
) -> tuple[IntakeCall, bool]:
    """Upsert the call row for this event. Returns (row, is_new).

    Idempotent on ``(provider, provider_call_id)``, which is the unique key.
    Every field is filled only when the event actually carries it: ``call_ended``
    brings the transcript, ``call_analyzed`` may bring a better one, and neither
    should blank what the other supplied.
    """
    call = find_call(db, provider=provider, provider_call_id=event.provider_call_id)
    is_new = call is None
    if call is None:
        call = IntakeCall(
            org_id=org_id,
            provider=provider,
            provider_call_id=event.provider_call_id,
            entry_point=_entry_point(event),
        )
        db.add(call)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            # Two webhooks for the same call landed at once. The other one won.
            db.expunge(call)
            found = find_call(db, provider=provider, provider_call_id=event.provider_call_id)
            if found is None:
                raise
            call, is_new = found, False

    _apply_event(call, event)

    if call.candidate_id is None:
        call.candidate_id = resolve_candidate_id(
            db, org_id=call.org_id, phone=event.counterparty_number
        )

    if event.kind == EVENT_STARTED and call.status == "":
        call.status = STATUS_IN_PROGRESS
    elif event.kind in {EVENT_ENDED, EVENT_ANALYZED} and call.status != STATUS_EXTRACTED:
        call.status = STATUS_COMPLETED

    db.flush()
    return call, is_new


def _apply_event(call: IntakeCall, event: CallEvent) -> None:
    if event.from_number:
        call.from_number = event.from_number
    if event.to_number:
        call.to_number = event.to_number
    if event.agent_id:
        call.raw = {**(call.raw or {}), "agent_id": event.agent_id}
    if event.started_at is not None:
        call.started_at = event.started_at
    if event.ended_at is not None:
        call.ended_at = event.ended_at
    if event.duration_seconds:
        call.duration_seconds = event.duration_seconds
    if event.recording_url:
        call.recording_url = event.recording_url
    # Never shorten a transcript. A late `call_analyzed` occasionally arrives
    # with a truncated one, and losing the conversation is unrecoverable.
    if event.transcript and len(event.transcript) >= len(call.transcript or ""):
        call.transcript = event.transcript
    if event.raw:
        call.raw = {**(call.raw or {}), "last_event": event.kind, "call": event.raw}

    # Consent arrives by whichever route the agent config provides: a dynamic
    # variable if one was set at call creation, otherwise the provider's
    # post-call analysis, which lands on `call_analyzed` rather than
    # `call_ended`. Both are checked because the agent definition owns that
    # choice and this code should not have to change when it does.
    #
    # Only ever latched true. A later event arriving without the field must not
    # silently withdraw a consent that was given, and false is the safe default
    # because a call with no recorded consent is not a consented call.
    if _consent_given(event):
        call.consent_recording = True


def _consent_given(event: CallEvent) -> bool:
    analysis = event.raw.get("call_analysis")
    custom = analysis.get("custom_analysis_data") if isinstance(analysis, dict) else None
    for source in (event.dynamic_variables, custom if isinstance(custom, dict) else {}):
        value = source.get("consent_recording")
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "yes"}:
            return True
    return False


def _entry_point(event: CallEvent) -> str:
    if event.call_type == "web_call":
        return "web"
    if event.direction == "outbound":
        return "outbound"
    return "dial_in"


def schedule_extraction(db: Session, call: IntakeCall) -> None:
    """Queue the extraction. Safe to call on every duplicate webhook.

    The idempotency key is the call row, so three provider retries produce one
    job. ``enqueue`` returns the existing row rather than inserting a second.
    """
    if call.status == STATUS_EXTRACTED:
        return
    if not (call.transcript or "").strip():
        # Nothing was said. A record would be noise, and the call row already
        # says it happened.
        return
    enqueue(
        db,
        org_id=call.org_id,
        kind=KIND_INTAKE_EXTRACT,
        idempotency_key=f"{KIND_INTAKE_EXTRACT}:{call.id}",
        payload={"intake_call_id": call.id},
    )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def extract_call(db: Session, call: IntakeCall) -> ExtractionResult | None:
    """Run the extractor over a finished call and persist the result.

    Idempotent: a call already marked extracted is left alone, so a requeued job
    cannot overwrite a record a human has since corrected.
    """
    if call.status == STATUS_EXTRACTED:
        return None
    transcript = (call.transcript or "").strip()
    if not transcript:
        call.status = STATUS_FAILED
        call.error = "no transcript"
        return None

    result = extract_intake(transcript, known=_known_for(db, call))
    state = result.coverage

    call.extracted = result.record
    call.extractor = result.extractor
    call.schema_version = SCHEMA_VERSION
    call.low_confidence = list(result.low_confidence)
    call.fields_missing = list(state.required_missing)
    call.required_complete = state.required_complete
    call.review_needed = result.review_needed
    call.review_reason = result.review_reason[:400]
    call.status = STATUS_EXTRACTED

    if call.candidate_id:
        upsert_preference(db, call=call, result=result)
    else:
        # The record is on the call and nothing is lost, but it cannot join the
        # match engine until a human says who this was. Auto-creating a
        # Candidate from a cold inbound call would put strangers in the CRM.
        call.review_needed = True
        call.review_reason = _join_reasons(call.review_reason, "caller not identified")

    record_event(
        db,
        org_id=call.org_id,
        type="intake.extracted",
        subject_type="intake_call",
        subject_id=call.id,
        payload={
            "candidate_id": call.candidate_id,
            "extractor": result.extractor,
            "required_complete": state.required_complete,
            "review_needed": call.review_needed,
        },
    )
    db.flush()
    return result


def upsert_preference(
    db: Session, *, call: IntakeCall, result: ExtractionResult
) -> CandidatePreference:
    """Write the preference row, one per candidate.

    Never clears a field that a human has confirmed: if ``confirmed_at`` is set,
    a later machine reading of another call is recorded on the call and the row
    is flagged rather than overwritten. A recruiter correcting a record and
    watching a re-run undo it is how people stop trusting the system.
    """
    record = result.record
    state = result.coverage
    row = db.scalar(
        select(CandidatePreference).where(CandidatePreference.candidate_id == call.candidate_id)
    )
    if row is None:
        row = CandidatePreference(org_id=call.org_id, candidate_id=call.candidate_id)
        db.add(row)
    elif row.confirmed_at is not None:
        row.review_needed = True
        row.review_reason = _join_reasons(
            row.review_reason, f"newer call {call.id} disagrees with a confirmed record"
        )
        db.flush()
        return row

    row.source_call_id = call.id
    row.spec = record.get("spec") or {}
    row.schema_version = SCHEMA_VERSION
    row.employment_status = record.get("employment_status") or ""
    row.reason_for_looking = record.get("reason_for_looking") or ""
    row.timeline = record.get("timeline") or ""
    row.urgency_driver = record.get("urgency_driver") or ""
    row.target_base = record.get("target_base")
    row.target_total = record.get("target_total")
    row.comp_currency = record.get("comp_currency") or ""
    row.comp_flexibility_notes = record.get("comp_flexibility_notes") or ""
    row.equity_importance = record.get("equity_importance") or ""
    row.work_authorization = (record.get("work_authorization") or "")[:120]
    row.willing_to_relocate = record.get("willing_to_relocate")
    row.active_processes = record.get("active_processes") or []
    row.offers_in_hand = record.get("offers_in_hand") or []
    row.highlights = record.get("highlights") or []
    row.confidential_items = record.get("confidential_items") or []
    row.ok_to_share_with_clients = record.get("ok_to_share_with_clients")
    row.declined = record.get("declined") or []
    row.low_confidence = list(result.low_confidence)
    row.fields_missing = list(state.required_missing)
    row.review_needed = result.review_needed
    row.review_reason = result.review_reason[:400]
    row.captured_at = call.ended_at or utcnow()
    db.flush()
    return row


def _known_for(db: Session, call: IntakeCall) -> str:
    """Pre-call facts, so the extractor does not bank them as call answers.

    A name we already had is not something the candidate said on this call, and
    counting it as covered would close the gate on a question nobody asked.
    """
    if not call.candidate_id:
        return known_context(phone=call.from_number)
    candidate = db.get(Candidate, call.candidate_id)
    if candidate is None:
        return known_context(phone=call.from_number)
    return known_context(
        name=candidate.full_name,
        email=candidate.primary_email or "",
        phone=call.from_number,
    )


def _join_reasons(existing: str, addition: str) -> str:
    parts = [p for p in [(existing or "").strip(), addition] if p]
    return "; ".join(parts)[:400]


# --------------------------------------------------------------------------
# mid-call coverage
# --------------------------------------------------------------------------


_NEEDED_GUIDANCE = "Ask about the topics in still_needed, one at a time, in order."

# What to do once the gate closes. Deliberately not a bare "hang up": the
# highlights question is the highest-value thing on the enrichment tier and a
# candidate who is still talking will usually answer it, so the agent is told it
# *may* spend one more question — and told to stop asking the moment the
# candidate sounds finished. Wrapping up is permission, not an instruction.
_WRAP_UP_GUIDANCE = (
    "Everything required is covered, so you may close whenever the candidate "
    "sounds ready. If they still sound willing, ask at most one topic from "
    "optional_topics first -- the projects question is the most valuable. Then "
    "recap the two or three most important facts for confirmation, tell them "
    "they will get a link to review their profile, and end the call."
)


def _question(key: str) -> dict[str, str]:
    from .schema import FIELDS_BY_KEY

    spec = FIELDS_BY_KEY[key]
    return {"topic": spec.key, "ask_about": spec.asks, "why_it_matters": spec.client_question}


def _snapshot(state, *, covered_count: int) -> dict[str, Any]:
    """Render a coverage state for a language model to act on.

    ``still_needed`` is required-tier only. Once the gate closes it goes empty
    and enrichment moves to ``optional_topics`` — reporting enrichment as
    "needed" would keep a finished call alive, which is the exact failure the
    tiering exists to prevent.
    """
    complete = state.required_complete
    needed = [] if complete else list(state.required_missing[:COVERAGE_QUESTION_LIMIT])
    optional = list(state.enrichment_missing[:COVERAGE_QUESTION_LIMIT]) if complete else []
    return {
        "wrap_up": complete,
        "still_needed": [_question(k) for k in needed],
        "optional_topics": [_question(k) for k in optional],
        "required_outstanding": len(state.required_missing),
        "already_covered": covered_count,
        "guidance": _WRAP_UP_GUIDANCE if complete else _NEEDED_GUIDANCE,
    }


def coverage_snapshot(transcript: str, *, known: str = "", fast: bool = True) -> dict[str, Any]:
    """What the agent still owes us, computed from what has actually been said.

    ``wrap_up`` is the whole point: it lets a cooperative call end in two or
    three minutes without the model having to guess how long it has been
    talking, and keeps an evasive one going without a timer forcing it shut.
    """
    result = extract_intake(transcript, known=known, fast=fast)
    return _snapshot(result.coverage, covered_count=len(result.coverage.covered))


def empty_coverage() -> dict[str, Any]:
    """Snapshot for a call with nothing said yet, without a model round trip."""
    return _snapshot(coverage({}), covered_count=0)

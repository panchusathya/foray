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
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enrich.normalize import split_name
from ..jobs import KIND_INTAKE_EXTRACT, enqueue
from ..models import (
    Candidate,
    CandidateEmail,
    CandidatePhone,
    CandidatePreference,
    IntakeCall,
    IntakeFieldAnswer,
    IntakeNumber,
    record_event,
    utcnow,
)
from ..outreach.suppressions import is_suppressed
from ..repository import (
    merge_contact_details,
    owner_of_email,
    sync_candidate_emails,
    sync_candidate_phones,
)
from .extract import ExtractionResult, extract_intake, known_context
from .phones import lookup_key
from .providers import (
    EVENT_ANALYZED,
    EVENT_ENDED,
    EVENT_STARTED,
    CallEvent,
    ProviderCallError,
    get_voice_provider,
)
from .schema import FIELDS, SCHEMA_VERSION, coverage, deferred_required, read_path

logger = logging.getLogger(__name__)

STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_EXTRACTED = "extracted"
STATUS_FAILED = "failed"

# Written when a call ends with nobody to attach it to, and cleared by
# ``attribute_call`` when a human says who it was. One constant so the writer
# and the reader cannot drift into two different strings -- the same rule
# ``messaging.identity.UNATTRIBUTED_REASON`` records.
UNIDENTIFIED_REASON = "caller not identified"

# The number is what the caller's *device* claims, and ANI is spoofable. So a
# call matched to a person by the email they spoke is attributed -- the record
# is theirs -- but the number is not written onto their row, because "somebody
# who knows Priya's address called from this phone" is not "this is Priya's
# phone", and a wrong row in ``candidate_phones`` points the dial-in line at
# the wrong person's comp and pipeline. ``attribute_call`` is where a human
# promotes it.
# No "; " inside the text: ``_join_reasons`` uses it as the separator, so a
# reason containing one splits into two segments and stops matching itself --
# which is how the first version of this left every attributed call on the
# review queue. ``test_no_reason_contains_the_separator`` pins it.
UNVERIFIED_NUMBER_REASON = "caller matched by email, number not added"

# An address arrived but the conversation captured nothing to attach to it.
NOTHING_CAPTURED_REASON = "caller identified but nothing was captured"


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


def counterparty_number(call: IntakeCall) -> str:
    """The number of the person on the other end, whichever way we dialled.

    On an inbound call that is ``from_number``. On an outbound one ``from_number``
    is *our* intake line, so reading it as the candidate's is how our own number
    ends up in ``candidate_phones`` -- pointing the dial-in line's caller
    lookup at whoever last had a row written.
    """
    return call.to_number if call.entry_point == "outbound" else call.from_number


def resolve_candidate_id(db: Session, *, org_id: str, phone: str) -> str | None:
    """Caller identification by number, scoped to the org.

    Authorises *accepting* an update. It does not authorise reading the record
    back to whoever dialled — ANI is spoofable, so disclosure needs the caller
    to confirm something only they would know. That gate lives in the agent
    prompt (Phase 4), not here, but it is the reason this returns an id and not
    a profile.

    The number is put through ``lookup_key`` first, because that is what wrote
    the column: ``repository.sync_candidate_phones`` stores keys, never raw
    input. Comparing a raw number against a key happened to work while Retell
    sent tidy E.164, and would have silently returned ``None`` -- an
    unattributed call, not an error -- the moment a provider sent
    ``(213) 431-7465`` or a bare national number. ``lookup_key`` still refuses
    to guess a country, so an unmatchable number remains unmatched rather than
    being attached to the wrong person.
    """
    key = lookup_key(phone)
    if not key:
        return None
    row = db.scalar(
        select(CandidatePhone).where(
            CandidatePhone.org_id == org_id, CandidatePhone.phone == key
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
        try:
            # `add` belongs INSIDE the savepoint, exactly as in `jobs.enqueue`.
            # Added outside it, a colliding flush poisons the outer transaction
            # instead of just the savepoint, and the next statement raises
            # PendingRollbackError -- so the recovery path cannot recover.
            with db.begin_nested():
                db.add(call)
                db.flush()
        except IntegrityError:
            # Two webhooks for the same call landed at once and the other one
            # won. Rolling the savepoint back has already expunged our pending
            # row, so do not expunge it again -- that raises InvalidRequestError
            # and turns a benign race into a 500, which the provider answers by
            # retrying. `jobs.enqueue` handles the identical collision the same
            # way: re-read and use the winner.
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

    if call.sms_opt_in:
        # The audit row behind the latch. The transcript on this call is the
        # primary evidence; this makes the grant findable without re-reading
        # every transcript, and pins the disclosure wording version. Idempotent
        # per (call, scope), so a re-delivered call_analyzed adds nothing.
        from ..messaging import consent as _consent

        _consent.record_consent(
            db,
            org_id=call.org_id,
            channel="voice",
            scope=_consent.SCOPE_APPLICATION_UPDATES,
            source=_consent.SOURCE_VOICE_WRAP_UP,
            disclosure_text=_consent.DISCLOSURE_VOICE_SMS,
            disclosure_version=_consent.DISCLOSURE_VOICE_SMS_VERSION,
            candidate_id=call.candidate_id,
            call_id=call.id,
        )

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
    # because a call with no recorded consent is not a consented call. The same
    # latch governs sms_opt_in: it is TCPA consent, and un-consenting happens by
    # the person replying STOP, never by a webhook omitting a field.
    if _analysis_flag(event, "consent_recording"):
        call.consent_recording = True
    if _analysis_flag(event, "sms_opt_in"):
        call.sms_opt_in = True


def _analysis_flag(event: CallEvent, key: str) -> bool:
    analysis = event.raw.get("call_analysis")
    custom = analysis.get("custom_analysis_data") if isinstance(analysis, dict) else None
    for source in (event.dynamic_variables, custom if isinstance(custom, dict) else {}):
        value = source.get(key)
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

    record_contact(call, result)
    link_caller(db, call)

    if call.candidate_id:
        upsert_preference(db, call=call, result=result)
    else:
        # The record is on the call and nothing is lost, but it cannot join the
        # match engine until a human says who this was. Auto-creating a
        # Candidate from a cold inbound call would put strangers in the CRM.
        call.review_needed = True
        call.review_reason = _join_reasons(call.review_reason, UNIDENTIFIED_REASON)

    write_field_answers(db, call=call, result=result)

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


def record_contact(call: IntakeCall, result: ExtractionResult) -> None:
    """Latch the caller's stated name and address onto the call.

    Latched, not assigned, for the reason ``consent_recording`` is: a replay
    against a better prompt, or a second pass that reads the transcript less
    well, must not blank an answer an earlier pass captured. There is nothing
    else on the row that records the caller said it.
    """
    if result.contact_name and not call.contact_name:
        call.contact_name = result.contact_name[:240]
    if result.contact_email and not call.contact_email:
        call.contact_email = result.contact_email[:255]


def link_caller(db: Session, call: IntakeCall) -> str | None:
    """Resolve an unattributed call to a candidate from what the caller said.

    The inbound agent has always asked for a name and the email they use with
    us. Until this existed the answer was transcribed and dropped, so every
    caller whose number was not already in ``candidate_phones`` produced an
    unattributed record -- a completed intake that the match engine could not
    see, waiting on a human to retype an address that was in the transcript.

    Three rules, in this order:

    **An existing owner of the address always wins.** Splitting somebody's
    history across two rows is worse than not creating one, which is the rule
    ``messaging.identity.link_candidate`` already follows.

    **An address is required to create anybody.** A name is not an identity --
    there are four Priya Ramans -- and a row created from one is a duplicate
    waiting to happen. This is the same bar the text channel sets, and the
    reason it sets it: an address identifies a person on its own.

    **Creating anybody at all needs a completed intake.** ``AGENTS.md`` records
    that a cold inbound call must not put a stranger in the CRM, and the text
    channel spends the same currency: requiring the gate to have closed makes a
    junk row cost a whole intake rather than one sentence. A caller who is new
    to us is asked to run the full intake anyway, so this is not a case the
    product avoids -- it is the case it is built for.

    Returns the candidate id, or None when the call stays unattributed.
    """
    if call.candidate_id:
        return call.candidate_id

    address = (call.contact_email or "").strip().lower()
    name = (call.contact_name or "").strip()
    if not address:
        if name:
            call.review_reason = _join_reasons(
                call.review_reason, "caller gave a name but no email"
            )
        return None

    owner = owner_of_email(db, org_id=call.org_id, email=address)
    if owner is not None:
        candidate = db.get(Candidate, owner)
        if candidate is not None:
            call.candidate_id = candidate.id
            # No phone: see UNVERIFIED_NUMBER_REASON.
            merge_contact_details(db, candidate, name=name)
            call.review_needed = True
            call.review_reason = _join_reasons(call.review_reason, UNVERIFIED_NUMBER_REASON)
            _track(db, candidate)
            return candidate.id

    if not coverage(call.extracted or {}).covered:
        # They gave an address and nothing else. A wrong number that ends with
        # "sorry -- oh, I'm Priya, priya@example.com" is not an intake, and a
        # row created from it is a stranger in the CRM under a real address.
        # Same guard ``attribute_thread`` applies before writing a preference,
        # and deliberately NOT "the gate closed": the heuristic path can hardly
        # ever close a gate, so that bar would read as "never" on an install
        # with no API key -- exactly the install this stack is built to run on.
        call.review_reason = _join_reasons(call.review_reason, NOTHING_CAPTURED_REASON)
        return None

    if is_suppressed(db, org_id=call.org_id, email=address):
        # On the do-not-contact list under an address we had not linked to a
        # row. Creating one now would put a person who asked to be left alone
        # back into the CRM as a fresh contact.
        call.review_reason = _join_reasons(
            call.review_reason, "caller is on the do-not-contact list"
        )
        return None

    number = counterparty_number(call)
    first, last = split_name(name)
    candidate = Candidate(
        org_id=call.org_id,
        source="intake-call",
        # Unique per person under uq_candidate_source and stable, so a second
        # call from the same person updates rather than duplicating. The address
        # rather than the number, because the address is what identified them.
        source_id=address,
        full_name=name or address,
        first_name=first,
        last_name=last,
        emails=[address],
        # Safe here in a way it is not on the branch above: this row exists
        # only because this caller described themselves, so the number that
        # placed the call is theirs by construction rather than by assertion.
        phones=[number] if number else [],
    )
    db.add(candidate)
    db.flush()
    sync_candidate_emails(db, candidate)
    sync_candidate_phones(db, candidate)
    call.candidate_id = candidate.id
    _track(db, candidate)
    record_event(
        db,
        org_id=call.org_id,
        type="intake.caller_identified",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"candidate_id": candidate.id, "created": True},
    )
    return candidate.id


def _track(db: Session, candidate: Candidate) -> None:
    """Put an intake-linked person on the default Contacts grid."""
    # Late import: crm -> outreach -> sequencer -> crm cycles at module load.
    from ..crm import track

    track(candidate)
    db.flush()


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
    _enqueue_preference_match(db, call, preference=row)
    return row


def _enqueue_preference_match(
    db: Session, call: IntakeCall, *, preference: CandidatePreference
) -> None:
    """Rescore against the jobs book. Uses preference.updated_at as the stamp."""
    if not call.candidate_id:
        return
    candidate = db.get(Candidate, call.candidate_id)
    if candidate is None:
        return
    from ..jobmatch.engine import enqueue_match_for_candidate

    enqueue_match_for_candidate(db, candidate, preference=preference)


def write_field_answers(db: Session, *, call: IntakeCall, result: ExtractionResult) -> None:
    """One row per field per call. Replaces any previous write for this call."""
    existing = db.scalars(
        select(IntakeFieldAnswer).where(IntakeFieldAnswer.intake_call_id == call.id)
    ).all()
    for row in existing:
        db.delete(row)
    record = result.record
    declined = {key for key in (record.get("declined") or []) if isinstance(key, str)}
    deferred = {key for key in (call.deferred_fields or []) if isinstance(key, str)}
    low = set(result.low_confidence)
    for spec in FIELDS:
        filled = spec.satisfied_by(record)
        values = [read_path(record, path) for path in spec.paths]
        value = next((item for item in values if item not in (None, "", [], {})), None)
        verbatim = ""
        if spec.key == "timeline":
            verbatim = (record.get("timeline_verbatim") or "")[:4000]
        elif spec.key == "ok_to_share_with_clients":
            verbatim = (record.get("ok_to_share_verbatim") or "")[:4000]
        asked = spec.key not in deferred
        db.add(
            IntakeFieldAnswer(
                org_id=call.org_id,
                intake_call_id=call.id,
                candidate_id=call.candidate_id,
                field_key=spec.key,
                value=value,
                verbatim=verbatim,
                confidence="low" if spec.key in low else ("high" if filled else ""),
                declined=spec.key in declined,
                asked=asked,
            )
        )
    db.flush()


def attribute_call(db: Session, *, call: IntakeCall, candidate_id: str) -> IntakeCall:
    """A human says who an unidentified caller was.

    This is the assertion caller ID cannot make. A recruiter picking a person
    off the review queue has listened to the call or read the transcript, so the
    number, the address and the name the caller gave all belong to that row --
    which is why this is the one path that writes the calling number into
    ``candidate_phones``, and therefore the one that makes the dial-in line
    recognise them next time.

    The address is still refused if somebody else owns it. That is not a
    contradiction of the above: a recruiter can be sure who they were talking
    to and still be about to staple the conversation to the wrong of two rows,
    and ``merge_contact_details`` reports the clash rather than moving an
    address between people. Same rule ``attribute_thread`` follows.
    """
    candidate = db.get(Candidate, candidate_id)
    if candidate is None or candidate.org_id != call.org_id:
        raise CallRequestError("That candidate is not in this workspace.")
    call.candidate_id = candidate.id
    merged = merge_contact_details(
        db,
        candidate,
        name=call.contact_name,
        email=call.contact_email,
        phone=counterparty_number(call),
    )
    if merged.conflicts:
        call.review_needed = True
        call.review_reason = _join_reasons(
            call.review_reason,
            f"already owned by somebody else: {', '.join(merged.conflicts)}",
        )
    _track(db, candidate)
    for row in db.scalars(
        select(IntakeFieldAnswer).where(IntakeFieldAnswer.intake_call_id == call.id)
    ):
        row.candidate_id = candidate.id
    if call.extracted:
        from .extract import ExtractionResult
        from .schema import coverage as coverage_of

        result = ExtractionResult(
            record=call.extracted,
            extractor=call.extractor or "replay",
            coverage=coverage_of(call.extracted),
            review_needed=call.review_needed,
            review_reason=call.review_reason,
        )
        upsert_preference(db, call=call, result=result)
    _clear_identity_review(call)
    db.flush()
    return call


# Every reason this function is allowed to retire. Attribution answers exactly
# one question -- who was that -- so it may clear only the flags raised by not
# knowing, and must leave a low-confidence bucket or a core-mode trim standing.
_IDENTITY_REASONS = frozenset(
    {
        UNIDENTIFIED_REASON,
        UNVERIFIED_NUMBER_REASON,
        "caller gave a name but no email",
        NOTHING_CAPTURED_REASON,
    }
)


def _clear_identity_review(call: IntakeCall) -> None:
    """Drop the identity reasons from the review note, keep the rest.

    ``review_reason`` used to hold one string and this was an equality check.
    It now holds joined segments -- a caller can be both unidentified and
    low-confidence on timeline -- so an equality check silently stopped
    clearing anything the moment a second reason appeared, and every attributed
    call stayed on the review queue forever.
    """
    remaining = [
        part
        for part in (call.review_reason or "").split("; ")
        if part and part not in _IDENTITY_REASONS
    ]
    call.review_reason = "; ".join(remaining)[:400]
    if not remaining:
        call.review_needed = False


def confirm_preference(
    db: Session, *, preference: CandidatePreference, confirmed_by: str
) -> CandidatePreference:
    preference.confirmed_at = utcnow()
    preference.confirmed_by = (confirmed_by or "recruiter")[:32]
    db.flush()
    return preference


def _known_for(db: Session, call: IntakeCall) -> str:
    """Pre-call facts, so the extractor does not bank them as call answers.

    A name we already had is not something the candidate said on this call, and
    counting it as covered would close the gate on a question nobody asked.
    """
    number = counterparty_number(call)
    if not call.candidate_id:
        return known_context(phone=number)
    candidate = db.get(Candidate, call.candidate_id)
    if candidate is None:
        return known_context(phone=number)
    return known_context(
        name=candidate.full_name,
        email=candidate.primary_email or "",
        phone=number,
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
    "a recruiter will follow up from a number they will recognise, thank them, "
    "and end the call."
)

_WRAP_UP_GUIDANCE_SMS = (
    "Everything required is covered, so you may close whenever the candidate "
    "sounds ready. If they still sound willing, ask at most one topic from "
    "optional_topics first -- the projects question is the most valuable. Then "
    "recap the two or three most important facts for confirmation, tell them "
    "they will get a text from the number they were called from, thank them, "
    "and end the call."
)

# Spoken only at these remaining-counts. Composed in code so the agent cannot
# invent a number, and left empty otherwise so it cannot nag.
PROGRESS_MILESTONES = frozenset({5, 3, 1})


def progress_line(questions_left: int) -> str:
    """Exact sentence to speak, or empty off-milestone."""
    if questions_left not in PROGRESS_MILESTONES:
        return ""
    if questions_left == 1:
        return "One more quick one and I'll let you go."
    words = {5: "Five", 3: "Three"}
    return f"{words[questions_left]} more quick ones and I'll let you go."


def core_only_for(minutes_available: int | None) -> bool:
    """A stated budget may only narrow the gate. None is today's full set."""
    if minutes_available is None:
        return False
    return minutes_available <= get_settings().intake_core_only_minutes


def parse_minutes_available(args: dict[str, Any] | None) -> int | None:
    if not args:
        return None
    raw = args.get("minutes_available")
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value


def apply_budget(call: IntakeCall, minutes_available: int | None) -> None:
    """Persist the budget on first sight. Later values do not overwrite."""
    if minutes_available is None:
        return
    if call.minutes_available is None:
        call.minutes_available = minutes_available
    if core_only_for(call.minutes_available):
        call.gate_mode = "core"
    else:
        call.gate_mode = "full"


def _wrap_up_guidance() -> str:
    return _WRAP_UP_GUIDANCE_SMS if get_settings().sms_enabled else _WRAP_UP_GUIDANCE


def _question(key: str) -> dict[str, str]:
    from .schema import FIELDS_BY_KEY

    spec = FIELDS_BY_KEY[key]
    return {"topic": spec.key, "ask_about": spec.asks, "why_it_matters": spec.client_question}


def _snapshot(state, *, covered_count: int, record: dict[str, Any] | None = None) -> dict[str, Any]:
    """Render a coverage state for a language model to act on.

    ``still_needed`` is required-tier only. Once the gate closes it goes empty
    and enrichment moves to ``optional_topics`` — reporting enrichment as
    "needed" would keep a finished call alive, which is the exact failure the
    tiering exists to prevent.

    ``progress_line`` is the exact sentence to speak, or empty. Composed here
    so the agent cannot invent a count.
    """
    complete = state.required_complete
    needed = [] if complete else list(state.required_missing[:COVERAGE_QUESTION_LIMIT])
    optional: list[str] = []
    if complete:
        # Under a core trim, ask must-have skills first if they never landed —
        # strongest retrieval signal, but not allowed to keep the gate open.
        if record is not None:
            leftover = deferred_required(record)
            if "spec.must_have_skills" in leftover:
                optional.append("spec.must_have_skills")
        optional.extend(state.enrichment_missing)
        optional = optional[:COVERAGE_QUESTION_LIMIT]
    outstanding = len(state.required_missing)
    return {
        "wrap_up": complete,
        "still_needed": [_question(k) for k in needed],
        "optional_topics": [_question(k) for k in optional],
        "required_outstanding": outstanding,
        "already_covered": covered_count,
        "progress_line": "" if complete else progress_line(outstanding),
        "guidance": _wrap_up_guidance() if complete else _NEEDED_GUIDANCE,
    }


class CallRequestError(RuntimeError):
    """We will not place this call. The message is safe to return to a caller."""


# How long before the same number may be dialled again. This endpoint turns one
# HTTP request into a ringing phone, so without a window it is a harassment tool
# and a metered one: a loop pointed at a stranger's number costs us per minute
# and costs them their afternoon.
REDIAL_COOLDOWN_MINUTES = 30


def request_intake_call(
    db: Session,
    *,
    org_id: str,
    phone: str,
    name: str = "",
    email: str = "",
    source: str = "website",
) -> IntakeCall:
    """Dial a candidate who asked to be called, and record the call up front.

    The row is written *before* the provider's first webhook arrives, which is
    what lets a web or outbound call resolve its org at all: there is no dialled
    number to look up, so the trusted source has to be a row we already own.
    Same shape as ``routers/track.py`` reading ``message.org_id``.

    Every refusal raises with a message intended for the caller, because the
    thing on the other end is a website form and "no" needs a reason.
    """
    target = lookup_key(phone)
    if not target:
        raise CallRequestError(
            "That does not look like a phone number we can dial. "
            "Please include the country code, e.g. +1 415 555 0123."
        )

    number = db.scalar(
        select(IntakeNumber).where(
            IntakeNumber.org_id == org_id,
            IntakeNumber.active.is_(True),
            IntakeNumber.provider == "retell",
        )
    )
    if number is None:
        raise CallRequestError("No intake number is configured for this workspace.")
    if target == number.phone:
        # Dialling ourselves connects the agent to the agent.
        raise CallRequestError("That is our own number.")

    candidate = _find_or_create_candidate(
        db, org_id=org_id, phone=target, name=name, email=email, source=source
    )

    # Do-not-contact is keyed on email, which is all the list has. It covers the
    # case that matters here -- a person already on it must not be dialled -- but
    # NOT an unknown number that has asked us to stop calling. That needs a
    # phone-aware suppression list before outbound volume starts.
    if is_suppressed(db, org_id=org_id, phone=target):
        raise CallRequestError("This contact is on the do-not-contact list.")
    for address in candidate.emails or []:
        if is_suppressed(db, org_id=org_id, email=address):
            raise CallRequestError("This contact is on the do-not-contact list.")

    recent = db.scalar(
        select(IntakeCall)
        .where(
            IntakeCall.org_id == org_id,
            IntakeCall.to_number == target,
            IntakeCall.created_at >= utcnow() - timedelta(minutes=REDIAL_COOLDOWN_MINUTES),
        )
        .order_by(IntakeCall.created_at.desc())
    )
    if recent is not None:
        raise CallRequestError(
            "We already called this number recently. Please try again in a little while."
        )

    settings = get_settings()
    provider = get_voice_provider()
    try:
        placed = provider.place_call(
            from_number=number.phone,
            to_number=target,
            agent_id=number.outbound_agent_id,
            metadata={"org_id": org_id, "candidate_id": candidate.id, "source": source},
            dynamic_variables={
                "candidate_first_name": candidate.first_name or "",
                "recruiter_name": settings.intake_recruiter_name,
                "sms_number_spoken": settings.intake_sms_number_spoken,
            },
        )
    except ProviderCallError as exc:
        logger.warning("intake dial failed for org %s: %s", org_id, exc)
        raise CallRequestError("We could not place the call just now. Please try again.") from exc

    call = IntakeCall(
        org_id=org_id,
        candidate_id=candidate.id,
        provider=provider.name,
        provider_call_id=placed.provider_call_id,
        entry_point="outbound",
        from_number=number.phone,
        to_number=target,
        status=STATUS_IN_PROGRESS,
        raw={"placed": placed.raw, "agent_id": placed.agent_id, "source": source},
    )
    db.add(call)
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="intake.call_requested",
        subject_type="intake_call",
        subject_id=call.id,
        payload={"candidate_id": candidate.id, "source": source},
    )
    return call


def _find_or_create_candidate(
    db: Session, *, org_id: str, phone: str, name: str, email: str, source: str
) -> Candidate:
    """Match the requester, or create them.

    Creating a Candidate here is right, and it is the opposite of the rule for a
    cold inbound call: this person filled in a form asking to be phoned. On the
    dial-in line we refuse to create anyone, because a stranger dialling a
    number has not asked to be in the CRM.
    """
    from ..crm import track

    existing_id = resolve_candidate_id(db, org_id=org_id, phone=phone)
    if existing_id:
        candidate = db.get(Candidate, existing_id)
        if candidate is not None:
            merge_contact_details(db, candidate, phone=phone, email=email, name=name)
            track(candidate)
            return candidate

    address = (email or "").strip().lower()
    if address:
        owner = db.scalar(
            select(CandidateEmail).where(
                CandidateEmail.org_id == org_id, CandidateEmail.email == address
            )
        )
        if owner is not None:
            candidate = db.get(Candidate, owner.candidate_id)
            if candidate is not None:
                merge_contact_details(db, candidate, phone=phone, email=address, name=name)
                track(candidate)
                return candidate

    first, last = split_name(name)
    candidate = Candidate(
        org_id=org_id,
        source=f"intake-{source}",
        # Unique per person under uq_candidate_source, and stable, so a second
        # request from the same number updates rather than duplicating.
        source_id=phone,
        full_name=name.strip() or phone,
        first_name=first,
        last_name=last,
        emails=[address] if address else [],
        phones=[phone],
    )
    db.add(candidate)
    db.flush()
    sync_candidate_emails(db, candidate)
    sync_candidate_phones(db, candidate)
    track(candidate)
    return candidate


def coverage_snapshot(
    transcript: str,
    *,
    known: str = "",
    fast: bool = True,
    minutes_available: int | None = None,
    call: IntakeCall | None = None,
) -> dict[str, Any]:
    """What the agent still owes us, computed from what has actually been said.

    ``wrap_up`` is the whole point: it lets a cooperative call end in two or
    three minutes without the model having to guess how long it has been
    talking, and keeps an evasive one going without a timer forcing it shut.
    """
    if call is not None:
        apply_budget(call, minutes_available)
        minutes_available = call.minutes_available
    core_only = core_only_for(minutes_available)
    result = extract_intake(transcript, known=known, fast=fast)
    state = coverage(result.record, core_only=core_only)
    if call is not None and core_only and state.required_complete:
        skipped = list(deferred_required(result.record))
        call.deferred_fields = skipped
        if skipped:
            call.review_needed = True
            call.review_reason = _join_reasons(
                call.review_reason, "core-mode trim; deferred fields unasked"
            )
    return _snapshot(state, covered_count=len(state.covered), record=result.record)


def empty_coverage() -> dict[str, Any]:
    """Snapshot for a call with nothing said yet, without a model round trip."""
    return _snapshot(coverage({}), covered_count=0)

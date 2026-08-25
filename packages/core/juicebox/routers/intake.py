"""Voice intake endpoints. Public, unauthenticated, signature-verified.

A phone call carries no principal — ``get_principal`` has nothing to read, and a
candidate dialling a number cannot present a bearer token. So these routes are
open to the internet, exactly like ``routers/track.py``, and the same rule
applies: **identity comes from a signature, and the org comes from a row we
already own, never from the request body.** An org id read out of a webhook
payload is a cross-tenant write with somebody else's phone bill attached.

Two endpoints, both signed with the same scheme:

* ``/v1/intake/webhook`` — the provider's call lifecycle. Verifies, records,
  queues, returns. It must not extract inline: the provider times out at ten
  seconds and retries three times, so a slow model call on the request path
  becomes four copies of the same conversation.
* ``/v1/intake/coverage`` — the agent's mid-call tool. Answers "what do I still
  need, and may I wrap up?" from the transcript so far. Read-only, and returns
  field names and questions rather than anything about a person, so it discloses
  nothing even though it is reachable by anyone holding a valid signature.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal, require_write
from ..intake.providers import (
    EVENT_ANALYZED,
    EVENT_ENDED,
    EVENT_IGNORED,
    EVENT_INBOUND,
    SignatureError,
    get_voice_provider,
    loads,
)
from ..intake.service import (
    CALLER_UNKNOWN,
    CallRequestError,
    apply_budget,
    attribute_call,
    confirm_preference,
    coverage_snapshot,
    empty_coverage,
    find_call,
    inbound_call_context,
    known_for_call,
    parse_minutes_available,
    prior_preference_record,
    record_call_event,
    request_intake_call,
    resolve_org_id,
    schedule_extraction,
)
from ..messaging.identity import AttributionError, attribute_thread
from ..messaging.threads import messages_of
from ..models import (
    CandidatePreference,
    IntakeCall,
    IntakeFieldAnswer,
    IntakeThread,
)
from ..schemas import (
    CandidatePreferenceOut,
    IntakeCallCountOut,
    IntakeCallOut,
    IntakeCallPatch,
    IntakeCallSummary,
    IntakeFieldAnswerOut,
    IntakeMessageOut,
    IntakeThreadCountOut,
    IntakeThreadOut,
    IntakeThreadPatch,
    IntakeThreadSummary,
)

logger = logging.getLogger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake", tags=["intake"])


async def raw_body(request: Request) -> bytes:
    """The unmodified request body.

    An async dependency so the handlers stay sync like the rest of the app:
    FastAPI resolves this on the loop, then runs the handler in a threadpool.

    It has to be the raw bytes. Re-serialising parsed JSON changes whitespace
    and key order, and every signature stops matching — the single most common
    way webhook verification is broken.
    """
    return await request.body()


def _verified(raw: bytes, signature: str):
    provider = get_voice_provider()
    try:
        provider.verify(raw_body=raw, signature=signature)
    except SignatureError as exc:
        # Deliberately terse to the caller, specific in the log. A verification
        # failure is either a misconfiguration or an attack, and neither is
        # helped by telling the sender which.
        logger.warning("intake signature rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
        ) from exc
    return provider


@router.post("/webhook")
def intake_webhook(
    raw: bytes = Depends(raw_body),
    x_retell_signature: str = Header(default="", alias="X-Retell-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    """Provider call lifecycle. Verify, record, queue, return."""
    provider = _verified(raw, x_retell_signature)
    event = provider.parse_event(loads(raw))

    if event.kind == EVENT_INBOUND:
        # Answered before the call-id check, because this event legitimately has
        # none -- the call does not exist yet. It is also the only event whose
        # *response* the provider reads, and it is on the critical path of a
        # ringing phone, so it does no writes and cannot 404.
        return _inbound_answer(db, provider_name=provider.name, event=event)

    if not event.provider_call_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No call id in payload"
        )
    if event.kind == EVENT_IGNORED:
        # Transcript updates and the transfer lifecycle land here. Acknowledged
        # so the provider stops retrying, and not recorded, because a provider
        # adding an event type must never start failing this endpoint.
        return {"status": "ignored", "call_id": event.provider_call_id}

    org_id = resolve_org_id(db, event, provider=provider.name)
    if org_id is None:
        # Refuse rather than guess. A 404 also surfaces the real cause: a number
        # taking calls that no org has claimed in intake_numbers.
        logger.warning(
            "intake call %s for unclaimed number %r (%s)",
            event.provider_call_id,
            event.our_number,
            event.call_type or "unknown type",
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No org owns this number"
        )

    call, created = record_call_event(db, org_id=org_id, provider=provider.name, event=event)
    queued = False
    if event.kind in {EVENT_ENDED, EVENT_ANALYZED}:
        schedule_extraction(db, call)
        queued = True

    return {
        "status": "accepted",
        "event": event.kind,
        "intake_call_id": call.id,
        "created": created,
        "extraction_queued": queued,
    }


def _inbound_answer(db: Session, *, provider_name: str, event) -> dict:
    """Tell the agent who is ringing, in the shape Retell reads back.

    Retell takes ``dynamic_variables`` from this response and interpolates them
    into the prompt before the agent opens its mouth. That timing is the entire
    feature: it is the only moment at which the line can know the caller without
    asking, so everything the returning-candidate branch needs has to be here.

    **Never fails the call.** A ringing phone is a person waiting, and a 4xx or
    an exception here makes Retell drop the call rather than answer it plainly.
    So an unclaimed number, an unknown caller and a broken lookup all resolve to
    the same thing -- the unknown-caller variables -- and the agent runs a normal
    first intake. The worst outcome is a returning candidate answering a couple
    of questions we already had; the alternative is a dead line.
    """
    org_id = resolve_org_id(db, event, provider=provider_name)
    if org_id is None:
        # Logged at warning like the lifecycle path, because it means a live
        # number is not claimed in intake_numbers -- but answered, not refused.
        logger.warning(
            "inbound ring for unclaimed number %r; answering as unknown caller",
            event.our_number,
        )
        variables = dict(CALLER_UNKNOWN)
    else:
        try:
            variables = inbound_call_context(db, org_id=org_id, phone=event.counterparty_number)
        except Exception:  # a ringing phone outranks anything this lookup can hit
            logger.exception("inbound caller lookup failed; answering as unknown caller")
            variables = dict(CALLER_UNKNOWN)

    return {"call_inbound": {"dynamic_variables": variables}}


class CallMeRequest(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    name: str = Field(default="", max_length=200)
    email: str = Field(default="", max_length=255)
    source: str = Field(default="website", max_length=40)


@router.post("/call-me", status_code=status.HTTP_202_ACCEPTED)
def call_me(
    payload: CallMeRequest,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Dial a candidate who asked to be called from the website.

    **Authenticated, unlike the other two routes.** This one turns a single HTTP
    request into a ringing phone at a number of the caller's choosing, which is
    a harassment tool and a metered one. There is no signature to lean on here
    because the request originates with us rather than the provider, so the
    marketing site's own backend calls this with an API key and the browser never
    touches it. A public version of this endpoint would need a per-page signed
    token and a CAPTCHA before it could exist at all.
    """
    try:
        call = request_intake_call(
            db,
            org_id=principal.org_id,
            phone=payload.phone,
            name=payload.name,
            email=payload.email,
            source=payload.source,
        )
    except CallRequestError as exc:
        # The message is written for a website form, so it is returned as-is.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    return {
        "status": "calling",
        "intake_call_id": call.id,
        "candidate_id": call.candidate_id,
        "to_number": call.to_number,
    }


@router.post("/coverage")
def intake_coverage(
    raw: bytes = Depends(raw_body),
    x_retell_signature: str = Header(default="", alias="X-Retell-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    """The mid-call gate: what is still owed, and whether to start closing.

    This is what makes call length a property of the record rather than of the
    model's sense of elapsed time. It is computed from the transcript so far, so
    the agent cannot talk itself into believing it has covered something.

    Degrades rather than fails. An error here is heard by a candidate as an
    agent going quiet, so an unresolvable call still gets a usable answer built
    from the transcript alone.
    """
    provider = _verified(raw, x_retell_signature)
    event, args = provider.parse_function_call(loads(raw))
    minutes_available = parse_minutes_available(args)

    transcript = (event.transcript or "").strip()
    call = find_call(db, provider=provider.name, provider_call_id=event.provider_call_id)
    # What other channels already captured about this person. Seeds the gate, so
    # a candidate who answered everything by text last week is asked what has
    # changed rather than the same eleven questions again.
    prior = prior_preference_record(db, call.candidate_id if call is not None else None)
    if not transcript:
        if call is not None:
            apply_budget(call, minutes_available)
        return empty_coverage(prior)

    # Pre-call *identity* facts must not count as call answers, or the gate
    # closes on questions nobody asked. Preferences are the opposite case and
    # go through `prior`.
    known = known_for_call(db, call)

    return coverage_snapshot(
        transcript,
        known=known,
        minutes_available=minutes_available,
        call=call,
        prior=prior,
    )


def _call_for_org(db: Session, call_id: str, org_id: str) -> IntakeCall:
    call = db.scalar(
        select(IntakeCall).where(IntakeCall.id == call_id, IntakeCall.org_id == org_id)
    )
    if call is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")
    return call


@router.get("/calls/count", response_model=IntakeCallCountOut)
def count_intake_calls(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    review_needed: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    unattributed: bool = Query(default=False),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
) -> IntakeCallCountOut:
    stmt = _calls_query(
        principal.org_id,
        review_needed=review_needed,
        status_filter=status_filter,
        unattributed=unattributed,
        created_after=created_after,
        created_before=created_before,
    )
    return IntakeCallCountOut(
        count=int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    )


@router.get("/calls", response_model=list[IntakeCallSummary])
def list_intake_calls(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    review_needed: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    unattributed: bool = Query(default=False),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[IntakeCallSummary]:
    stmt = _calls_query(
        principal.org_id,
        review_needed=review_needed,
        status_filter=status_filter,
        unattributed=unattributed,
        created_after=created_after,
        created_before=created_before,
    )
    rows = db.scalars(
        stmt.order_by(IntakeCall.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return [IntakeCallSummary.model_validate(row) for row in rows]


def _calls_query(
    org_id: str,
    *,
    review_needed: bool | None,
    status_filter: str | None,
    unattributed: bool,
    created_after: datetime | None,
    created_before: datetime | None,
):
    stmt = select(IntakeCall).where(IntakeCall.org_id == org_id)
    if review_needed is not None:
        stmt = stmt.where(IntakeCall.review_needed.is_(review_needed))
    if status_filter:
        stmt = stmt.where(IntakeCall.status == status_filter)
    if unattributed:
        stmt = stmt.where(IntakeCall.candidate_id.is_(None))
    if created_after is not None:
        stmt = stmt.where(IntakeCall.created_at >= created_after)
    if created_before is not None:
        stmt = stmt.where(IntakeCall.created_at < created_before)
    return stmt


# Threads live on the same review queue as calls. Without these the queue is
# voice-only, and because IntakeThread.review_needed defaults to true every
# chat thread is flagged and nothing shows it.
def _threads_query(
    org_id: str,
    *,
    review_needed: bool | None,
    status_filter: str | None,
    unattributed: bool,
    created_after: datetime | None,
    created_before: datetime | None,
):
    stmt = select(IntakeThread).where(IntakeThread.org_id == org_id)
    if review_needed is not None:
        stmt = stmt.where(IntakeThread.review_needed.is_(review_needed))
    if status_filter:
        stmt = stmt.where(IntakeThread.status == status_filter)
    if unattributed:
        stmt = stmt.where(IntakeThread.candidate_id.is_(None))
    if created_after is not None:
        stmt = stmt.where(IntakeThread.created_at >= created_after)
    if created_before is not None:
        stmt = stmt.where(IntakeThread.created_at < created_before)
    return stmt


@router.get("/threads/count", response_model=IntakeThreadCountOut)
def count_intake_threads(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    review_needed: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    unattributed: bool = Query(default=False),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
) -> IntakeThreadCountOut:
    stmt = _threads_query(
        principal.org_id,
        review_needed=review_needed,
        status_filter=status_filter,
        unattributed=unattributed,
        created_after=created_after,
        created_before=created_before,
    )
    return IntakeThreadCountOut(
        count=int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    )


@router.get("/threads", response_model=list[IntakeThreadSummary])
def list_intake_threads(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
    review_needed: bool | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    unattributed: bool = Query(default=False),
    created_after: datetime | None = Query(default=None),
    created_before: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[IntakeThreadSummary]:
    stmt = _threads_query(
        principal.org_id,
        review_needed=review_needed,
        status_filter=status_filter,
        unattributed=unattributed,
        created_after=created_after,
        created_before=created_before,
    )
    rows = db.scalars(
        stmt.order_by(IntakeThread.created_at.desc()).offset(offset).limit(limit)
    ).all()
    return [IntakeThreadSummary.model_validate(row) for row in rows]


def _thread_for_org(db: Session, thread_id: str, org_id: str) -> IntakeThread:
    row = db.scalar(
        select(IntakeThread).where(IntakeThread.id == thread_id, IntakeThread.org_id == org_id)
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such thread")
    return row


def _thread_out(db: Session, thread: IntakeThread) -> IntakeThreadOut:
    return IntakeThreadOut(
        **IntakeThreadSummary.model_validate(thread).model_dump(),
        messages=[IntakeMessageOut.model_validate(m) for m in messages_of(db, thread)],
        record=dict(thread.record or {}),
        fields_missing=list(thread.fields_missing or []),
    )


@router.get("/threads/{thread_id}", response_model=IntakeThreadOut)
def get_intake_thread(
    thread_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> IntakeThreadOut:
    """Full messaging thread for the recruiter, including message bodies."""
    thread = _thread_for_org(db, thread_id, principal.org_id)
    return _thread_out(db, thread)


@router.patch("/threads/{thread_id}", response_model=IntakeThreadSummary)
def patch_intake_thread(
    thread_id: str,
    payload: IntakeThreadPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> IntakeThreadSummary:
    """Attach an unidentified conversation to a person."""
    thread = _thread_for_org(db, thread_id, principal.org_id)
    try:
        attribute_thread(db, thread=thread, candidate_id=payload.candidate_id)
    except AttributionError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return IntakeThreadSummary.model_validate(thread)


@router.get("/calls/{call_id}", response_model=IntakeCallOut)
def get_intake_call(
    call_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> IntakeCallOut:
    call = _call_for_org(db, call_id, principal.org_id)
    return _call_out(db, call)


@router.patch("/calls/{call_id}", response_model=IntakeCallOut)
def patch_intake_call(
    call_id: str,
    payload: IntakeCallPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> IntakeCallOut:
    call = _call_for_org(db, call_id, principal.org_id)
    try:
        attribute_call(db, call=call, candidate_id=payload.candidate_id)
    except CallRequestError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _call_out(db, call)


@router.get("/preferences/{candidate_id}", response_model=CandidatePreferenceOut)
def get_preference(
    candidate_id: str,
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> CandidatePreferenceOut:
    row = _preference_for_org(db, candidate_id, principal.org_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preference not found")
    return CandidatePreferenceOut.model_validate(row)


@router.post("/preferences/{candidate_id}/confirm", response_model=CandidatePreferenceOut)
def confirm_preference_route(
    candidate_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> CandidatePreferenceOut:
    row = _preference_for_org(db, candidate_id, principal.org_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preference not found")
    confirm_preference(db, preference=row, confirmed_by=principal.user_id)
    return CandidatePreferenceOut.model_validate(row)


def _preference_for_org(db: Session, candidate_id: str, org_id: str) -> CandidatePreference | None:
    return db.scalar(
        select(CandidatePreference).where(
            CandidatePreference.candidate_id == candidate_id,
            CandidatePreference.org_id == org_id,
        )
    )


def _call_out(db: Session, call: IntakeCall) -> IntakeCallOut:
    payload = IntakeCallOut.model_validate(call)
    answers = db.scalars(
        select(IntakeFieldAnswer)
        .where(
            IntakeFieldAnswer.intake_call_id == call.id,
            IntakeFieldAnswer.org_id == call.org_id,
        )
        .order_by(IntakeFieldAnswer.field_key)
    ).all()
    pref = None
    if call.candidate_id:
        pref = _preference_for_org(db, call.candidate_id, call.org_id)
    return payload.model_copy(
        update={
            "field_answers": [IntakeFieldAnswerOut.model_validate(row) for row in answers],
            "preference": CandidatePreferenceOut.model_validate(pref) if pref else None,
        }
    )

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

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, get_db
from ..intake.providers import (
    EVENT_ANALYZED,
    EVENT_ENDED,
    EVENT_IGNORED,
    SignatureError,
    get_voice_provider,
    loads,
)
from ..intake.service import (
    coverage_snapshot,
    empty_coverage,
    find_call,
    record_call_event,
    resolve_org_id,
    schedule_extraction,
)
from ..models import Candidate

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
    event, _args = provider.parse_function_call(loads(raw))

    transcript = (event.transcript or "").strip()
    if not transcript:
        return empty_coverage()

    known = ""
    call = find_call(db, provider=provider.name, provider_call_id=event.provider_call_id)
    if call is not None and call.candidate_id:
        # Pre-call facts must not count as call answers, or the gate closes on
        # questions nobody asked.
        candidate = db.get(Candidate, call.candidate_id)
        if candidate is not None:
            known = f"name: {candidate.full_name}"

    return coverage_snapshot(transcript, known=known)

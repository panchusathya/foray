"""Candidate-facing chat, and the token that lets a website open one.

Two routers with very different postures.

``router`` is ordinary recruiter-authenticated API: it hands an org the signed
token their marketing site embeds.

``chat_router`` is **unauthenticated by design** -- a stranger on a careers page
has no principal, exactly as an email client opening a tracking pixel has none.
It follows ``routers/track.py``: every request carries an HMAC we minted, the
org is taken from the *verified* half of that token and never from the body,
and an unverifiable request is refused rather than served a default. The org is
then loaded to confirm it still exists, so a signature over a deleted org does
not open a conversation into nothing.

The thread token is scoped to one thread and one purpose, so it cannot be
replayed to open conversations against the org, and knowing a thread id is not
enough to read or extend somebody else's intake.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..intake.phones import lookup_key
from ..intake.service import resolve_candidate_id
from ..jobs import KIND_INTAKE_TURN, enqueue
from ..messaging import prompts
from ..messaging.channels import SMS, WEB, OutboundMessage, SmsChannel
from ..messaging.conversation import _deliver, run_turn
from ..messaging.schemas import (
    ChatMessageIn,
    ChatMessageOut,
    ChatSessionOut,
    ChatStartIn,
    WidgetTokenOut,
)
from ..messaging.sms import SignatureError as TwilioSignatureError
from ..messaging.sms import parse_inbound, resolve_sms_org_id, verify_request
from ..messaging.suppression import looks_like_help
from ..messaging.threads import find_active_thread, messages_of, open_thread, record_inbound
from ..models import IntakeThread, Org, utcnow
from ..security import signed_intake_token, verify_intake_token

logger = logging.getLogger(__name__)

PURPOSE_WIDGET = "intake-widget"
PURPOSE_THREAD = "intake-thread"

# A single conversation cannot be used as an unbounded write endpoint. Per-IP
# limiting belongs at the edge (nginx limit_req in packages/web/nginx.conf);
# this is the durable floor counted per org rather than per IP.
MAX_INBOUND_PER_THREAD = 200
MAX_NEW_THREADS_PER_ORG_PER_HOUR = 20
_THREAD_CAP_WINDOW = timedelta(hours=1)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake", tags=["intake"])
chat_router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake/chat", tags=["intake"])
sms_router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake/sms", tags=["intake"])

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing intake token"
)


@router.get("/widget-token", response_model=WidgetTokenOut)
def get_widget_token(
    principal: Principal = Depends(get_principal),
) -> WidgetTokenOut:
    """The token this org's website embeds to open intake chats."""
    return WidgetTokenOut(
        token=f"{principal.org_id}.{signed_intake_token(PURPOSE_WIDGET, principal.org_id)}"
    )


def _org_from_widget_token(db: Session, token: str) -> Org:
    # rpartition, not split: an org id never contains a dot but a signature is
    # base64url, and splitting on the first dot would corrupt both halves the
    # day that changes.
    org_id, _, signature = (token or "").rpartition(".")
    if not org_id or not signature:
        raise _UNAUTHORIZED
    if not verify_intake_token(PURPOSE_WIDGET, org_id, signature):
        raise _UNAUTHORIZED
    org = db.get(Org, org_id)
    if org is None:
        raise _UNAUTHORIZED
    return org


def _thread_from_token(db: Session, thread_id: str, token: str) -> IntakeThread:
    if not verify_intake_token(PURPOSE_THREAD, thread_id, token):
        raise _UNAUTHORIZED
    thread = db.get(IntakeThread, thread_id)
    if thread is None:
        raise _UNAUTHORIZED
    return thread


def _session_out(db: Session, thread: IntakeThread, *, action: str = "") -> ChatSessionOut:
    return ChatSessionOut(
        thread_id=thread.id,
        thread_token=signed_intake_token(PURPOSE_THREAD, thread.id),
        status=thread.status,
        action=action,
        required_complete=bool(thread.required_complete),
        fields_missing=list(thread.fields_missing or []),
        messages=[ChatMessageOut.model_validate(m) for m in messages_of(db, thread)],
    )


def _check_thread_creation_cap(db: Session, *, org_id: str) -> None:
    """Refuse when this org has opened too many widget conversations recently."""
    since = utcnow() - _THREAD_CAP_WINDOW
    count = (
        db.scalar(
            select(func.count())
            .select_from(IntakeThread)
            .where(
                IntakeThread.org_id == org_id,
                IntakeThread.entry_point == "web_widget",
                IntakeThread.created_at >= since,
            )
        )
        or 0
    )
    if count >= MAX_NEW_THREADS_PER_ORG_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many new conversations for this organization",
        )


@chat_router.post("/sessions", response_model=ChatSessionOut, status_code=201)
def start_session(payload: ChatStartIn, db: Session = Depends(get_db)) -> ChatSessionOut:
    """Open a conversation and send the greeting plus the first question."""
    org = _org_from_widget_token(db, payload.token)
    _check_thread_creation_cap(db, org_id=org.id)
    thread = open_thread(
        db,
        org_id=org.id,
        channel=WEB,
        entry_point="web_widget",
        # The person opened the widget and started typing. There is no
        # unsolicited message here to have consented to.
        consent_messaging=True,
    )
    result = run_turn(db, thread=thread)
    return _session_out(db, thread, action=result.action)


@chat_router.get("/sessions/{thread_id}", response_model=ChatSessionOut)
def read_session(
    thread_id: str,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
) -> ChatSessionOut:
    """Resume. A week-old thread comes back exactly where it stopped."""
    thread = _thread_from_token(db, thread_id, token)
    return _session_out(db, thread)


@chat_router.post("/sessions/{thread_id}/messages", response_model=ChatSessionOut)
def post_message(
    thread_id: str,
    payload: ChatMessageIn,
    db: Session = Depends(get_db),
) -> ChatSessionOut:
    """Record an inbound message and run one turn.

    Synchronous, unlike the SMS path, because a person is watching a typing
    indicator -- ``llm.json_fast`` exists for exactly this. The work itself is
    ``conversation.run_turn``, the same function the queued job calls, so the
    two entry points cannot drift.

    A repeated ``client_message_id`` returns the conversation unchanged. That
    is the whole retry story: a double-tapped send button, or a browser
    replaying a request it never saw the response to, must not produce a second
    extraction and a second question.
    """
    thread = _thread_from_token(db, thread_id, payload.token)

    if thread.inbound_count >= MAX_INBOUND_PER_THREAD:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This conversation has reached its message limit",
        )

    _, created = record_inbound(
        db,
        thread=thread,
        body=payload.body,
        provider_native_id=payload.client_message_id,
    )
    if not created:
        return _session_out(db, thread, action="duplicate")

    result = run_turn(db, thread=thread)
    return _session_out(db, thread, action=result.action)


def _resolve_or_open_sms_thread(
    db: Session,
    *,
    org_id: str,
    from_address: str,
    to_address: str,
) -> IntakeThread:
    from_key = lookup_key(from_address) or from_address
    to_key = lookup_key(to_address) or to_address
    thread = find_active_thread(db, channel=SMS, from_address=from_key)
    if thread is not None:
        return thread
    if not SmsChannel().is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SMS channel is not configured",
        )
    candidate_id = resolve_candidate_id(db, org_id=org_id, phone=from_key)
    return open_thread(
        db,
        org_id=org_id,
        channel=SMS,
        provider="twilio",
        entry_point="sms_inbound",
        candidate_id=candidate_id,
        from_address=from_key,
        to_address=to_key,
        consent_messaging=True,
    )


@sms_router.post("/webhook")
async def sms_webhook(request: Request, db: Session = Depends(get_db)) -> dict:
    """Twilio inbound SMS. Verify, record, queue, return."""
    form = await request.form()
    params = {key: str(value) for key, value in form.multi_items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    try:
        verify_request(url=str(request.url), params=params, signature=signature)
    except TwilioSignatureError as exc:
        logger.warning("Twilio signature rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
        ) from exc

    inbound = parse_inbound(params)
    if not inbound.provider_native_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No MessageSid in payload"
        )

    org_id = resolve_sms_org_id(db, to_address=inbound.to_address)
    if org_id is None:
        logger.warning("SMS to unclaimed or SMS-disabled number %r", inbound.to_address)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No org owns this number"
        )

    thread = _resolve_or_open_sms_thread(
        db,
        org_id=org_id,
        from_address=inbound.from_address,
        to_address=inbound.to_address,
    )

    message, created = record_inbound(
        db,
        thread=thread,
        body=inbound.body,
        provider_native_id=inbound.provider_native_id,
        raw=inbound.raw,
    )
    if not created:
        return {"status": "accepted", "duplicate": True, "message_id": message.id}

    if looks_like_help(inbound.body):
        _deliver(db, thread=thread, message=OutboundMessage(body=prompts.HELP_REPLY))
        return {"status": "accepted", "help": True, "message_id": message.id}

    enqueue(
        db,
        org_id=org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{message.id}",
        payload={"thread_id": thread.id},
    )
    return {"status": "accepted", "message_id": message.id, "thread_id": thread.id}

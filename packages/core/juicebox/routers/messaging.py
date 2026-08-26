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

import json
import logging
from datetime import timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import CommittingRoute, Principal, get_db, get_principal
from ..intake.phones import lookup_key
from ..intake.service import resolve_candidate_id
from ..jobs import KIND_INTAKE_ATTACHMENT, KIND_INTAKE_TURN, enqueue
from ..messaging import consent, prompts
from ..messaging.channels import LINQ, WEB, LinqChannel, OutboundMessage
from ..messaging.conversation import deliver, run_turn
from ..messaging.linq import (
    POLL_EVENTS,
    REACTION_EVENTS,
    STATUS_EVENTS,
    parse_inbound,
    parse_reaction,
    parse_status_event,
    resolve_linq_org_id,
    verify_webhook,
)
from ..messaging.linq import SignatureError as LinqSignatureError
from ..messaging.schemas import (
    ChatMessageIn,
    ChatMessageOut,
    ChatSessionOut,
    ChatStartIn,
    WidgetTokenOut,
)
from ..messaging.suppression import handle_stop, looks_like_help, looks_like_stop
from ..messaging.threads import (
    apply_status_event,
    find_active_thread,
    find_by_provider_id,
    messages_of,
    open_thread,
    record_inbound,
)
from ..models import IntakeMessage, IntakeThread, Org, utcnow
from ..security import signed_intake_token, verify_intake_token

logger = logging.getLogger(__name__)

PURPOSE_WIDGET = "intake-widget"
PURPOSE_THREAD = "intake-thread"

# A single conversation cannot be used as an unbounded write endpoint. Per-IP
# limiting belongs at the edge (nginx limit_req in packages/web/nginx.conf);
# this is the durable floor counted per org rather than per IP.
MAX_INBOUND_PER_THREAD = 200
MAX_NEW_THREADS_PER_ORG_PER_HOUR = 20
MAX_NEW_THREADS_PER_SENDER_PER_HOUR = 5
_THREAD_CAP_WINDOW = timedelta(hours=1)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake", tags=["intake"])
chat_router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake/chat", tags=["intake"])
linq_router = APIRouter(route_class=CommittingRoute, prefix="/v1/intake/linq", tags=["intake"])
# The resume-upload page a texted link opens. Unauthenticated like the chat
# routes and gated the same way: a purpose-scoped HMAC bound to one thread.
upload_router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/intake/uploads", tags=["intake"]
)
# Contact-card vCards Linq fetches as media. Unauthenticated like uploads:
# a purpose-scoped HMAC bound to one proposal, no principal.
jobcard_router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/intake/jobcards", tags=["intake"]
)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing intake token"
)


async def raw_body(request: Request) -> bytes:
    """Unmodified request body for webhook signature verification."""
    return await request.body()


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
    """Refuse when this org has opened too many public conversations recently."""
    since = utcnow() - _THREAD_CAP_WINDOW
    count = (
        db.scalar(
            select(func.count())
            .select_from(IntakeThread)
            .where(
                IntakeThread.org_id == org_id,
                IntakeThread.entry_point.in_(("web_widget", "linq_inbound")),
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


def _check_sender_thread_cap(
    db: Session, *, org_id: str, from_address: str, channel: str
) -> None:
    """Refuse when one sender has opened too many conversations recently."""
    if not from_address:
        return
    since = utcnow() - _THREAD_CAP_WINDOW
    count = (
        db.scalar(
            select(func.count())
            .select_from(IntakeThread)
            .where(
                IntakeThread.org_id == org_id,
                IntakeThread.channel == channel,
                IntakeThread.from_address == from_address,
                IntakeThread.created_at >= since,
            )
        )
        or 0
    )
    if count >= MAX_NEW_THREADS_PER_SENDER_PER_HOUR:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many new conversations from this sender",
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
    consent.record_consent(
        db,
        org_id=org.id,
        channel=WEB,
        scope=consent.SCOPE_MESSAGING,
        source=consent.SOURCE_WEB_WIDGET,
        disclosure_text=consent.DISCLOSURE_WEB_WIDGET,
        disclosure_version=consent.DISCLOSURE_WEB_WIDGET_VERSION,
        thread_id=thread.id,
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


def _resolve_or_open_linq_thread(
    db: Session,
    *,
    org_id: str,
    inbound,
) -> IntakeThread:
    from ..messaging.linq import InboundLinq

    assert isinstance(inbound, InboundLinq)
    from_key = lookup_key(inbound.from_address) or inbound.from_address
    thread = find_by_provider_id(
        db, provider="linq", provider_thread_id=inbound.chat_id
    )
    if thread is not None:
        return thread
    thread = find_active_thread(db, channel=LINQ, from_address=from_key)
    if thread is not None:
        if inbound.chat_id and not thread.provider_thread_id:
            thread.provider_thread_id = inbound.chat_id
        return thread
    _check_sender_thread_cap(db, org_id=org_id, from_address=from_key, channel=LINQ)
    if not LinqChannel().is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Linq channel is not configured",
        )
    to_key = lookup_key(inbound.to_address) or inbound.to_address
    candidate_id = resolve_candidate_id(db, org_id=org_id, phone=from_key)
    thread = open_thread(
        db,
        org_id=org_id,
        channel=LINQ,
        provider="linq",
        provider_thread_id=inbound.chat_id,
        entry_point="linq_inbound",
        candidate_id=candidate_id,
        from_address=from_key,
        to_address=to_key,
        consent_messaging=True,
        # Mode stays "intake" here on purpose. Onboarding is driven by `flow`
        # (open_thread defaults a Linq thread to linkedin_short), and the
        # thread flips to "managing" when that flow wraps up -- so this line
        # setting a mode would only be a second, competing opinion about
        # which phase the conversation is in.
    )
    consent.record_consent(
        db,
        org_id=org_id,
        channel=LINQ,
        scope=consent.SCOPE_MESSAGING,
        source=consent.SOURCE_INBOUND_TEXT,
        disclosure_text=consent.DISCLOSURE_INBOUND_TEXT,
        disclosure_version=consent.DISCLOSURE_INBOUND_TEXT_VERSION,
        candidate_id=candidate_id,
        thread_id=thread.id,
    )
    return thread


_UPLOAD_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Send your resume</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
         display: grid; place-items: center; min-height: 100vh; background: #f6f6f4; }}
  main {{ background: #fff; border-radius: 16px; padding: 28px 24px; margin: 16px;
          max-width: 420px; box-shadow: 0 2px 12px rgba(0,0,0,.08); }}
  h1 {{ font-size: 20px; margin: 0 0 8px; }}
  p {{ color: #555; font-size: 14px; margin: 0 0 20px; }}
  input[type=file] {{ width: 100%; margin-bottom: 16px; }}
  button {{ width: 100%; padding: 14px; font-size: 16px; border: 0; border-radius: 10px;
            background: #1a3c34; color: #fff; }}
  .ok {{ color: #1a7f4f; font-weight: 600; }}
</style>
<main>
  <h1>Send your resume</h1>
  <p>PDF or Word (.docx), up to 10&nbsp;MB. It goes straight to your
     recruiter&#8217;s intake &mdash; you can head back to the conversation after.</p>
  <form method="post" enctype="multipart/form-data" action="?token={token}">
    <input type="file" name="file" accept=".pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document" required>
    <button type="submit">Upload</button>
  </form>
</main>
"""

_UPLOAD_DONE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Resume received</title>
<style>
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0;
         display: grid; place-items: center; min-height: 100vh; background: #f6f6f4; }
  main { background: #fff; border-radius: 16px; padding: 28px 24px; margin: 16px;
         max-width: 420px; box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }
  h1 { font-size: 20px; color: #1a7f4f; }
  p { color: #555; font-size: 14px; }
</style>
<main>
  <h1>Got it &#10003;</h1>
  <p>Your resume is in. You can close this tab and head back to the conversation.</p>
</main>
"""


def _upload_thread(db: Session, thread_id: str, token: str) -> IntakeThread:
    from ..messaging.uploads import verify_upload_token

    if not verify_upload_token(thread_id, token):
        raise _UNAUTHORIZED
    thread = db.get(IntakeThread, thread_id)
    if thread is None or thread.status == "stopped":
        raise _UNAUTHORIZED
    return thread


@upload_router.get("/{thread_id}")
def upload_page(thread_id: str, token: str = Query(default=""), db: Session = Depends(get_db)):
    """The page the texted link opens. A GET has no side effects, so a link
    preview prefetch cannot consume anything."""
    from fastapi.responses import HTMLResponse

    _upload_thread(db, thread_id, token)
    return HTMLResponse(_UPLOAD_PAGE.format(token=token))


@upload_router.post("/{thread_id}")
async def upload_resume(
    thread_id: str,
    file: UploadFile = File(...),
    token: str = Query(default=""),
    db: Session = Depends(get_db),
):
    """Store the resume against the thread's candidate and queue the parse.

    Requires the thread to be linkable (an email or LinkedIn captured) -- the
    agent only sends this link after onboarding collects one, and a public
    endpoint that created candidate rows from nothing would let anyone fill
    the CRM by tapping a forwarded link.
    """
    from fastapi.responses import HTMLResponse

    from ..docgen.parse import sniff_resume_type
    from ..messaging import identity
    from ..messaging.attachments import store_thread_resume
    from .documents import _read_capped

    thread = _upload_thread(db, thread_id, token)

    candidate_id = thread.candidate_id or identity.link_candidate(db, thread=thread)
    if not candidate_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="We need your name and email first — reply in the conversation.",
        )

    data = await _read_capped(file)
    content_type = sniff_resume_type(data)
    if not content_type:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only PDF or Word (.docx) files are accepted",
        )

    store_thread_resume(
        db,
        thread=thread,
        candidate_id=candidate_id,
        data=data,
        filename=file.filename or "",
        content_type=content_type,
    )
    return HTMLResponse(_UPLOAD_DONE, status_code=201)


_NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


@jobcard_router.get("/{proposal_id}.vcf")
def jobcard_vcf(
    proposal_id: str, token: str = Query(default=""), db: Session = Depends(get_db)
):
    """The vCard Linq fetches for a job-match contact card.

    A GET has no side effects, so a link preview prefetch cannot consume
    anything. Bad token and unknown proposal are the same 404 so an id is
    not confirmable by probing.
    """
    from ..messaging.jobcards import verify_jobcard_token
    from ..models import JobMatch, JobPosting, JobProposal
    from ..presentation.jobcard import jobcard
    from ..presentation.logo import contact_photo, photo_for, posting_domain
    from ..presentation.vcard import build_vcard, vcard_filename

    if not verify_jobcard_token(proposal_id, token):
        raise _NOT_FOUND
    proposal = db.get(JobProposal, proposal_id)
    if proposal is None:
        raise _NOT_FOUND
    posting = db.get(JobPosting, proposal.job_posting_id)
    if posting is None:
        raise _NOT_FOUND
    match = db.get(JobMatch, proposal.job_match_id) if proposal.job_match_id else None
    card = jobcard(posting, match)
    if proposal.reasons:
        card = {**card, "reasons": list(proposal.reasons)}
    try:
        photo = photo_for(posting_domain(posting), posting.company or "")
        payload = build_vcard(card, photo_png=photo)
    except Exception:  # noqa: BLE001 - never 500 over a favicon
        logger.warning("jobcard render fell back to monogram for %s", proposal_id)
        photo = contact_photo(None, company=posting.company or "")
        payload = build_vcard(card, photo_png=photo)
    filename = vcard_filename(card)
    return Response(
        content=payload,
        media_type="text/vcard",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@linq_router.post("/webhook")
def linq_webhook(
    raw: bytes = Depends(raw_body),
    webhook_id: str = Header(default="", alias="webhook-id"),
    webhook_timestamp: str = Header(default="", alias="webhook-timestamp"),
    webhook_signature: str = Header(default="", alias="webhook-signature"),
    db: Session = Depends(get_db),
) -> dict:
    """Linq inbound message.received. Verify, record, queue, return."""
    try:
        verify_webhook(
            raw_body=raw,
            webhook_id=webhook_id,
            timestamp=webhook_timestamp,
            signature=webhook_signature,
        )
    except LinqSignatureError as exc:
        logger.warning("Linq signature rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
        ) from exc

    settings = get_settings()
    if not settings.linq_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Linq intake is not enabled",
        )
    if not LinqChannel().is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Linq channel is not configured",
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid JSON"
        ) from exc

    event_type = str(payload.get("event_type") or "")
    if event_type in STATUS_EVENTS:
        return _handle_linq_status(db, payload, event_type=event_type)
    if event_type in REACTION_EVENTS:
        return _handle_linq_reaction(db, payload)
    if event_type in POLL_EVENTS:
        return _handle_linq_poll(db, payload, event_type=event_type)
    if event_type != "message.received":
        # Logged, not silent. An unrecognised event answered 200 with no trace,
        # so "Linq is delivering webhooks" and "we are dropping every one of
        # them" looked identical from the outside -- the same shape of hole
        # that hid the Retell key being absent.
        logger.info("Linq event %r ignored (not message.received)", event_type)
        return {"status": "ignored", "event_type": event_type}

    try:
        inbound = parse_inbound(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    if inbound.is_group:
        logger.info("Linq group chat %r ignored", inbound.chat_id)
        return {"status": "ignored", "reason": "group_chat"}

    if not inbound.provider_native_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No message id in payload"
        )

    org_id = resolve_linq_org_id(db, to_address=inbound.to_address)
    if org_id is None:
        logger.warning("Linq message to unclaimed or inactive number %r", inbound.to_address)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No org owns this number"
        )

    thread = find_by_provider_id(
        db, provider="linq", provider_thread_id=inbound.chat_id
    )
    if thread is None:
        _check_thread_creation_cap(db, org_id=org_id)
        thread = _resolve_or_open_linq_thread(db, org_id=org_id, inbound=inbound)

    if thread.inbound_count >= MAX_INBOUND_PER_THREAD:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="This conversation has reached its message limit",
        )

    message, created = record_inbound(
        db,
        thread=thread,
        body=inbound.body,
        provider_native_id=inbound.provider_native_id,
        raw=inbound.raw,
    )
    if not created:
        logger.info("Linq message %r already recorded", inbound.provider_native_id)
        return {"status": "accepted", "duplicate": True, "message_id": message.id}

    if looks_like_stop(inbound.body):
        handle_stop(db, thread=thread)
        # The single confirmation a carrier expects, and the last thing this
        # thread will ever send. The web path acks through run_turn's STOP
        # branch; this path returns before the turn, so it has to ack here or
        # a texter who opts out gets silence where every other channel
        # confirms. Routed through `deliver` deliberately: `run_turn` refuses
        # to send to a stopped thread, which is the correct rule everywhere
        # except this one message.
        deliver(db, thread=thread, message=OutboundMessage(body=prompts.STOP_ACKNOWLEDGEMENT))
        return {"status": "accepted", "stopped": True, "message_id": message.id}

    if looks_like_help(inbound.body):
        deliver(db, thread=thread, message=OutboundMessage(body=prompts.HELP_REPLY))
        return {"status": "accepted", "help": True, "message_id": message.id}

    if inbound.attachments:
        # The attachment job owns this message's whole consequence — it runs
        # the turn itself when no resume comes of the download. Enqueueing a
        # turn here too would race it and double-reply.
        enqueue(
            db,
            org_id=org_id,
            kind=KIND_INTAKE_ATTACHMENT,
            idempotency_key=f"{KIND_INTAKE_ATTACHMENT}:{message.id}",
            payload={
                "thread_id": thread.id,
                "message_id": message.id,
                "attachments": [a.to_dict() for a in inbound.attachments],
                "had_text": bool(inbound.body.strip()),
            },
        )
        return {"status": "accepted", "attachments": True, "message_id": message.id}

    if not inbound.body.strip():
        return {"status": "accepted", "ignored": True, "message_id": message.id}

    enqueue(
        db,
        org_id=org_id,
        kind=KIND_INTAKE_TURN,
        idempotency_key=f"{KIND_INTAKE_TURN}:{message.id}",
        payload={"thread_id": thread.id},
    )
    return {"status": "accepted", "message_id": message.id, "thread_id": thread.id}


def _handle_linq_poll(db, payload: dict, *, event_type: str) -> dict:
    """Legacy poll lifecycle: we no longer send polls, but a candidate may
    still tap one already delivered. ``poll.sent`` maps option ids onto
    ``last_presented`` for in-flight frames that still carry ``option_text``;
    ``poll.vote.added`` enqueues the apply. New frames have neither field.
    """
    from ..messaging.linq import parse_poll_options, parse_poll_vote

    if event_type == "poll.sent":
        chat_id, options = parse_poll_options(payload)
        thread = find_by_provider_id(db, provider="linq", provider_thread_id=chat_id)
        if thread is None or not options:
            return {"status": "ignored", "reason": "unknown_chat_or_no_options"}
        frame = [dict(entry) for entry in (thread.last_presented or [])]
        by_text = {text.strip(): option_id for option_id, text in options}
        matched = 0
        for entry in frame:
            option_id = by_text.get((entry.get("option_text") or "").strip())
            if option_id:
                entry["option_id"] = option_id
                matched += 1
        # Reassigned, not mutated: SQLAlchemy does not see in-place JSON edits.
        thread.last_presented = frame
        db.flush()
        logger.info("poll options mapped for thread %s: %d/%d", thread.id, matched, len(frame))
        return {"status": "accepted", "mapped": matched}

    if event_type != "poll.vote.added":
        # A removed vote is not an instruction to withdraw an application --
        # by then it has been submitted, and un-tapping cannot unsend it.
        logger.info("Linq %s noted, not acted on", event_type)
        return {"status": "accepted", "acted": False}

    chat_id, option_id, is_me = parse_poll_vote(payload)
    if is_me or not option_id:
        return {"status": "accepted", "acted": False, "reason": "own_vote_or_no_option"}
    thread = find_by_provider_id(db, provider="linq", provider_thread_id=chat_id)
    if thread is None:
        return {"status": "ignored", "reason": "unknown_chat"}

    entry = next(
        (e for e in (thread.last_presented or []) if e.get("option_id") == option_id), None
    )
    if entry is None:
        logger.info("poll vote for an option we cannot resolve: %r", option_id)
        return {"status": "accepted", "acted": False, "reason": "unknown_option"}

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        # One application per option, however many times the tap is delivered.
        idempotency_key=f"{KIND_INTAKE_TURN}:poll:{option_id}",
        payload={"thread_id": thread.id, "apply_proposal_id": entry.get("proposal_id")},
    )
    logger.info("poll vote queued an apply for proposal %s", entry.get("proposal_id"))
    return {"status": "accepted", "acted": True, "proposal_id": entry.get("proposal_id")}


def _handle_linq_reaction(db, payload: dict) -> dict:
    """A thumbs-up on a job card is a yes to that job.

    The card it lands on is the whole point: each role goes out in its own
    message, and the proposal row remembers which message carried it, so a
    reaction resolves to exactly one posting. Reacting to a bubble is the
    cheapest possible "apply to this" -- no typing, no ordinal to get wrong.

    Anything that is not an unambiguous yes from the candidate is recorded and
    ignored. Submitting an application off a `laugh` would be acting on a
    guess about somebody's intent, and an application cannot be unsent.
    """
    from ..models import JobProposal

    try:
        event = parse_reaction(payload)
    except ValueError as exc:
        logger.info("Linq reaction ignored: %s", exc)
        return {"status": "ignored", "reason": str(exc)}

    if not event.is_positive:
        logger.info(
            "Linq reaction %r (%s, from_me=%s) noted, not acted on",
            event.reaction_type,
            event.event_type,
            event.is_from_me,
        )
        return {"status": "accepted", "acted": False, "reaction": event.reaction_type}

    thread = find_by_provider_id(db, provider="linq", provider_thread_id=event.chat_id)
    if thread is None:
        logger.info("Linq reaction on an unknown chat %r", event.chat_id)
        return {"status": "ignored", "reason": "unknown_chat"}

    outbound = db.scalar(
        select(IntakeMessage).where(
            IntakeMessage.thread_id == thread.id,
            IntakeMessage.provider_native_id == event.message_id,
        )
    )
    if outbound is None:
        logger.info("Linq reaction on a message we did not send: %r", event.message_id)
        return {"status": "ignored", "reason": "unknown_message"}

    proposal = db.scalar(
        select(JobProposal).where(
            JobProposal.thread_id == thread.id,
            JobProposal.message_id == outbound.id,
        )
    )
    if proposal is None:
        logger.info("Linq reaction on a message carrying no job card")
        return {"status": "accepted", "acted": False, "reason": "not_a_job_card"}

    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_INTAKE_TURN,
        # Keyed on the reaction, so a tapback delivered twice applies once.
        idempotency_key=f"{KIND_INTAKE_TURN}:reaction:{event.message_id}",
        payload={"thread_id": thread.id, "apply_proposal_id": proposal.id},
    )
    logger.info("Linq thumbs-up on proposal %s queued as an apply", proposal.id)
    return {"status": "accepted", "acted": True, "proposal_id": proposal.id}


def _handle_linq_status(db, payload: dict, *, event_type: str) -> dict:
    """Stamp delivered/read/failed onto the outbound row. Never starts a turn."""
    try:
        event = parse_status_event(payload)
    except ValueError as exc:
        logger.warning("Linq status event %s ignored: %s", event_type, exc)
        return {"status": "ignored", "event_type": event_type, "reason": str(exc)}
    row = apply_status_event(
        db,
        provider_native_id=event.message_id,
        event_type=event.event_type,
        delivered_at=event.delivered_at,
        read_at=event.read_at,
        error=event.error,
    )
    if row is None:
        return {"status": "ignored", "event_type": event_type, "reason": "unknown_message"}
    return {
        "status": "accepted",
        "event_type": event_type,
        "message_id": row.id,
        "delivery_status": row.status,
    }

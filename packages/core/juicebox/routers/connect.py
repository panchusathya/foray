"""The candidate connect-your-apps surface: page, OAuth start, callback,
disconnect.

Unauthenticated by nature — the person is a stranger in a text thread — so
every thread-addressed route is guarded by the purpose-scoped HMAC from
``messaging/connect_links.py``, and the OAuth round trip carries a short-lived
signed state token, the ``routers/integrations.py`` pattern with its own role
so a candidate state can never be replayed against the recruiter callback.

Callback paths stay per-provider and stable (``/v1/connect/google/callback``)
because they are registered with the provider — fixed API surface.

Two hard rules the handlers keep:

* **GET has no side effects.** iMessage prefetches links for previews; the
  page renders from existing rows only. State is minted on the /start
  redirect and written on the provider callback.
* **The org comes from the thread row, never the request.** Same reason as
  every other intake surface: there is no principal here.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..connectors import ConnectorError, configured_connectors, get_connector
from ..deps import CommittingRoute, get_db
from ..integrations.crypto import encrypt_json
from ..jobs import KIND_CONNECTOR_INDEX, enqueue
from ..messaging.connect_links import verify_connect_token
from ..messaging.consent import record_connect_consent, revoke_connect_consent
from ..models import CandidateConnection, IntakeThread, new_id, record_event, utcnow
from ..security import TokenError, create_access_token, decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(route_class=CommittingRoute, prefix="/v1/connect", tags=["connect"])

_STATE_ROLE = "candidate-connect"
_STATE_TTL_SECONDS = 600

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired link"
)


def _thread_or_401(db: Session, thread_id: str, token: str) -> IntakeThread:
    if not verify_connect_token(thread_id, token):
        raise _UNAUTHORIZED
    thread = db.get(IntakeThread, thread_id)
    if thread is None or thread.status == "stopped":
        raise _UNAUTHORIZED
    return thread


def _page(title: str, body_html: str) -> HTMLResponse:
    """Every response page, under the same locked-down CSP the mailbox OAuth
    callback uses. ``body_html`` is built here from escaped pieces only —
    nothing request-supplied reaches it unescaped."""
    return HTMLResponse(
        "<!doctype html><html><head><meta name='viewport' "
        "content='width=device-width, initial-scale=1'><style>"
        "body{font-family:system-ui;background:#080D0B;color:#EAF0EC;margin:0;"
        "display:grid;place-items:center;min-height:100vh}"
        "main{max-width:420px;padding:32px;text-align:center}"
        "h1{font-size:20px;margin-bottom:8px}"
        "p{color:#9DB1A6;font-size:14px;line-height:1.5}"
        "a.button{display:block;margin:10px 0;padding:14px;border-radius:10px;"
        "background:#1E2B24;color:#EAF0EC;text-decoration:none;font-size:15px}"
        "form{margin:6px 0}"
        "button{width:100%;padding:10px;border-radius:10px;border:1px solid #32463B;"
        "background:transparent;color:#9DB1A6;font-size:13px;cursor:pointer}"
        "</style></head><body><main>"
        f"<h1>{html.escape(title)}</h1>{body_html}"
        "</main></body></html>",
        headers={
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get("/{thread_id}", include_in_schema=False)
def connect_page(
    thread_id: str, token: str = Query(default=""), db: Session = Depends(get_db)
) -> HTMLResponse:
    """The page the texted link opens: the disclosure, one button per
    configured connector, and the state of anything already connected."""
    from ..messaging.consent import DISCLOSURE_CONNECT

    thread = _thread_or_401(db, thread_id, token)
    connections = {
        row.provider: row
        for row in db.scalars(
            select(CandidateConnection).where(CandidateConnection.thread_id == thread.id)
        )
    }
    parts: list[str] = [f"<p>{html.escape(DISCLOSURE_CONNECT)}</p>"]
    offered = configured_connectors()
    if not offered:
        parts.append("<p>No apps are available to connect right now.</p>")
    for connector in offered:
        existing = connections.get(connector.name)
        if existing is not None and existing.status == "active":
            parts.append(
                f"<p>&#10003; {html.escape(connector.label)} connected as "
                f"{html.escape(existing.address)}</p>"
                f"<form method='post' action='/v1/connect/{html.escape(thread.id)}/"
                f"{html.escape(connector.name)}/disconnect?token={html.escape(token)}'>"
                f"<button type='submit'>disconnect {html.escape(connector.label)}</button>"
                "</form>"
            )
            continue
        parts.append(
            f"<a class='button' href='/v1/connect/{html.escape(thread.id)}/"
            f"{html.escape(connector.name)}/start?token={html.escape(token)}'>"
            f"Connect {html.escape(connector.label)}</a>"
        )
    return _page("Connect an app", "".join(parts))


@router.get("/{thread_id}/{provider_name}/start", include_in_schema=False)
def start_connect(
    thread_id: str,
    provider_name: str,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    thread = _thread_or_401(db, thread_id, token)
    try:
        connector = get_connector(provider_name)
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    if not connector.is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{provider_name} is not configured",
        )
    state = create_access_token(
        subject=thread.id,
        org_id=thread.org_id,
        role=_STATE_ROLE,
        ttl_seconds=_STATE_TTL_SECONDS,
        extra={"provider": provider_name},
    )
    return RedirectResponse(connector.authorize_url(state), status_code=302)


@router.get("/{provider_name}/callback", include_in_schema=False)
def oauth_callback(
    provider_name: str,
    request: Request,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if error:
        logger.warning("%s connect consent failed: %s %s", provider_name, error, error_description)
        return _page(
            "Connection cancelled",
            f"<p>{html.escape(error_description or f'{provider_name} did not grant access.')}</p>",
        )
    try:
        connector = get_connector(provider_name)
    except ConnectorError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    try:
        payload = decode_access_token(state)
        if payload.get("role") != _STATE_ROLE:
            raise TokenError("wrong token type")
        if payload.get("provider", provider_name) != provider_name:
            raise TokenError("state was minted for a different provider")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid state: {exc}"
        ) from exc

    thread = db.get(IntakeThread, payload["sub"])
    if thread is None or thread.org_id != payload["org"] or thread.status == "stopped":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown thread")

    try:
        grant = connector.exchange_code(code, dict(request.query_params))
    except ConnectorError as exc:
        logger.error("%s connect failed for thread %s: %s", provider_name, thread.id, exc)
        return _page("Connection failed", f"<p>{html.escape(str(exc))}</p>")

    connection = db.scalar(
        select(CandidateConnection).where(
            CandidateConnection.thread_id == thread.id,
            CandidateConnection.provider == provider_name,
        )
    )
    if connection is None:
        connection = CandidateConnection(org_id=thread.org_id, thread_id=thread.id)
        db.add(connection)
    connection.provider = provider_name
    connection.candidate_id = thread.candidate_id
    connection.address = grant.address
    connection.display_name = grant.display_name
    connection.provider_account_id = grant.provider_account_id
    connection.scopes = grant.scopes
    connection.token_blob = encrypt_json(grant.tokens)
    connection.status = "active"
    connection.error = ""

    consent = record_connect_consent(db, thread=thread, provider=provider_name)
    connection.consent_id = consent.id

    # A provider-verified address may fill an empty contact_email — it is the
    # strongest identity signal a thread ever sees — but never overwrite a
    # stated one: ownership questions belong to identity.link_candidate.
    if "@" in grant.address and not (thread.contact_email or "").strip():
        thread.contact_email = grant.address[:255]

    run = new_id("run")
    connection.index_state = {"status": "queued", "run": run, "queued_at": utcnow().isoformat()}
    db.flush()
    enqueue(
        db,
        org_id=thread.org_id,
        kind=KIND_CONNECTOR_INDEX,
        # Fresh run id per callback: a reconnect must index again, so the key
        # is never fixed per connection (the mailbox-backfill lesson).
        idempotency_key=f"{KIND_CONNECTOR_INDEX}:{connection.id}:{run}",
        payload={"connection_id": connection.id, "run": run},
    )
    record_event(
        db,
        org_id=thread.org_id,
        type="connector.connected",
        subject_type="candidate_connection",
        subject_id=connection.id,
        payload={"provider": provider_name},
    )
    return _page(
        "Connected",
        f"<p>{html.escape(connector.label)} is connected. Head back to your "
        "texts — I'm reading it now, this takes under a minute.</p>",
    )


@router.post("/{thread_id}/{provider_name}/disconnect", include_in_schema=False)
def disconnect(
    thread_id: str,
    provider_name: str,
    token: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    thread = _thread_or_401(db, thread_id, token)
    connection = db.scalar(
        select(CandidateConnection).where(
            CandidateConnection.thread_id == thread.id,
            CandidateConnection.provider == provider_name,
        )
    )
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not connected")
    try:
        get_connector(provider_name).revoke(db, connection)
    except Exception as exc:  # noqa: BLE001 - our copy is wiped regardless
        logger.warning("%s upstream revoke failed for %s: %s", provider_name, connection.id, exc)
    connection.status = "revoked"
    connection.token_blob = ""
    connection.error = ""
    revoke_connect_consent(db, thread_id=thread.id, provider=provider_name)
    record_event(
        db,
        org_id=thread.org_id,
        type="connector.disconnected",
        subject_type="candidate_connection",
        subject_id=connection.id,
        payload={"provider": provider_name},
    )
    return _page(
        "Disconnected",
        "<p>Access removed and our copy of the connection deleted. The resume "
        "and answers you already shared stay unless you ask us to remove them.</p>",
    )

"""Connect external mailboxes (Microsoft Graph / Outlook).

The callback is unauthenticated by necessity — the browser arrives from
Microsoft's login page — so it carries a short-lived signed state token minted
at connect time instead of a bearer header.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import Principal, get_db, get_principal, require_write
from ..integrations import microsoft
from ..integrations.crypto import encrypt_json
from ..models import MailboxConnection, record_event
from ..schemas import MailboxConnectionOut, MicrosoftConnectOut, MicrosoftIntegrationOut
from ..security import TokenError, create_access_token, decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/integrations/microsoft", tags=["integrations"])

_STATE_ROLE = "msconnect"
_STATE_TTL_SECONDS = 600


def _not_configured() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Microsoft integration is not configured; set MS_CLIENT_ID and MS_CLIENT_SECRET",
    )


@router.get("", response_model=MicrosoftIntegrationOut)
def integration_status(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> MicrosoftIntegrationOut:
    rows = db.scalars(
        select(MailboxConnection)
        .where(
            MailboxConnection.org_id == principal.org_id,
            MailboxConnection.provider == "microsoft",
        )
        .order_by(MailboxConnection.created_at.desc())
    ).all()
    return MicrosoftIntegrationOut(
        configured=microsoft.is_configured(),
        redirect_uri=microsoft.redirect_uri(),
        connections=[MailboxConnectionOut.model_validate(row) for row in rows],
    )


@router.post("/connect", response_model=MicrosoftConnectOut)
def start_connect(principal: Principal = Depends(require_write)) -> MicrosoftConnectOut:
    if not microsoft.is_configured():
        raise _not_configured()
    if not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connect a mailbox from a logged-in user session, not an API key",
        )
    state = create_access_token(
        subject=principal.user_id,
        org_id=principal.org_id,
        role=_STATE_ROLE,
        ttl_seconds=_STATE_TTL_SECONDS,
    )
    return MicrosoftConnectOut(auth_url=microsoft.authorize_url(state))


def _callback_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><body style='font-family:system-ui;background:#080D0B;"
        "color:#EAF0EC;display:grid;place-items:center;height:100vh;margin:0'>"
        f"<div style='text-align:center'><h2>{title}</h2><p>{body}</p></div>"
        "</body></html>"
    )


@router.get("/callback", include_in_schema=False)
def oauth_callback(
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if error:
        logger.warning("microsoft consent failed: %s %s", error, error_description)
        return _callback_page(
            "Connection cancelled", error_description or "Microsoft did not grant access."
        )
    if not microsoft.is_configured():
        raise _not_configured()

    try:
        payload = decode_access_token(state)
        if payload.get("role") != _STATE_ROLE:
            raise TokenError("wrong token type")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid state: {exc}"
        ) from exc

    org_id, user_id = payload["org"], payload["sub"]
    try:
        tokens = microsoft.exchange_code(code)
        profile = microsoft.fetch_profile(tokens["access_token"])
    except microsoft.MicrosoftError as exc:
        logger.error("microsoft connect failed: %s", exc)
        return _callback_page("Connection failed", str(exc))

    if not profile["address"]:
        return _callback_page("Connection failed", "Microsoft returned no mailbox address.")

    connection = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.address == profile["address"],
        )
    )
    if connection is None:
        connection = MailboxConnection(org_id=org_id, address=profile["address"])
        db.add(connection)
    connection.user_id = user_id
    connection.display_name = profile["display_name"]
    connection.ms_user_id = profile["ms_user_id"]
    connection.token_blob = encrypt_json(tokens)
    connection.status = "active"
    connection.error = ""
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="integration.mailbox_connected",
        subject_type="mailbox",
        subject_id=connection.id,
        payload={"address": connection.address, "provider": "microsoft"},
    )
    return _callback_page(
        "Outlook connected",
        f"{connection.address} can now send outreach. You can close this tab.",
    )


@router.delete("/{connection_id}")
def disconnect(
    connection_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    connection = db.get(MailboxConnection, connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    connection.status = "revoked"
    connection.token_blob = ""
    connection.error = ""
    db.flush()
    record_event(
        db,
        org_id=principal.org_id,
        type="integration.mailbox_disconnected",
        subject_type="mailbox",
        subject_id=connection.id,
        payload={"address": connection.address},
    )
    return {"revoked": connection.id}

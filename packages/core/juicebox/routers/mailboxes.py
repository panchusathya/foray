"""Connect and disconnect Microsoft Outlook mailboxes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..deps import Principal, get_db, get_principal, require_write
from ..models import MailboxConnection
from ..outreach.graph import (
    GraphError,
    authorize_url,
    exchange_code,
    fetch_profile,
    get_graph_transport,
    upsert_connection,
)
from ..schemas import MailboxListOut, MailboxOut, MicrosoftAuthorizeOut
from ..security import TokenError, create_signed_state, decode_signed_state

router = APIRouter(prefix="/v1/mailboxes", tags=["mailboxes"])


def _frontend_origin() -> str:
    settings = get_settings()
    if settings.cors_origins:
        return settings.cors_origins[0].rstrip("/")
    return settings.base_url.rstrip("/")


def _callback_redirect(**params: str) -> RedirectResponse:
    target = f"{_frontend_origin()}/outreach?{urlencode(params)}"
    return RedirectResponse(url=target, status_code=status.HTTP_302_FOUND)


@router.get("", response_model=MailboxListOut)
def list_mailboxes(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> MailboxListOut:
    settings = get_settings()
    rows = db.scalars(
        select(MailboxConnection)
        .where(MailboxConnection.org_id == principal.org_id)
        .order_by(MailboxConnection.created_at.desc())
    ).all()
    return MailboxListOut(
        configured=settings.graph_configured,
        env_mailbox=settings.graph_mailbox,
        connections=[MailboxOut.model_validate(row) for row in rows],
    )


@router.get("/microsoft/authorize", response_model=MicrosoftAuthorizeOut)
def microsoft_authorize(
    principal: Principal = Depends(require_write),
) -> MicrosoftAuthorizeOut:
    if principal.kind != "user" or not principal.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connect Outlook from a signed-in user, not an API key",
        )
    state = create_signed_state(
        {"purpose": "microsoft_oauth", "org": principal.org_id, "sub": principal.user_id}
    )
    try:
        url = authorize_url(state=state)
    except GraphError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return MicrosoftAuthorizeOut(url=url, state=state)


@router.get("/microsoft/callback")
def microsoft_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    db: Session = Depends(get_db),
):
    """Browser landing pad after Azure AD consent. Unauthenticated on purpose."""
    if error:
        return _callback_redirect(mailbox="error", reason=error_description or error)

    if not code or not state:
        return _callback_redirect(mailbox="error", reason="missing_code")

    try:
        payload = decode_signed_state(state)
    except TokenError:
        return _callback_redirect(mailbox="error", reason="invalid_state")

    if payload.get("purpose") != "microsoft_oauth":
        return _callback_redirect(mailbox="error", reason="invalid_state")

    org_id = str(payload.get("org") or "")
    user_id = str(payload.get("sub") or "")
    if not org_id:
        return _callback_redirect(mailbox="error", reason="invalid_state")

    transport = get_graph_transport()
    try:
        tokens = exchange_code(transport, code)
        profile = fetch_profile(transport, str(tokens["access_token"]))
        upsert_connection(
            db,
            org_id=org_id,
            user_id=user_id or None,
            token_payload=tokens,
            profile=profile,
        )
    except GraphError as exc:
        return _callback_redirect(mailbox="error", reason=str(exc)[:200])

    return _callback_redirect(mailbox="connected")


@router.delete("/{mailbox_id}")
def disconnect_mailbox(
    mailbox_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    row = db.get(MailboxConnection, mailbox_id)
    if row is None or row.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found")
    db.delete(row)
    db.flush()
    return {"disconnected": mailbox_id}

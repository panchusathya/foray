"""Deliverability: seed inboxes, seed tests and per-domain inbox health.

Measurement only (step 1). Nothing here pauses or re-routes outreach; "Test
now" queues seed tests that obey the same rails as real sends. A seed's
password goes in once, is verified by signing in, and never comes back out of
any response.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deliverability import seed_oauth
from ..deliverability import seeds as seeds_module
from ..deliverability.overview import build_overview, custom_domains, refresh_domain, seed_out
from ..deliverability.probes import INTERVALS, check_seed, queue_test_now
from ..deliverability.seeds import IMAP_PRESETS, SeedReadError
from ..deps import (
    OAUTH_STATE_ROLE,
    CommittingRoute,
    Principal,
    get_db,
    get_principal,
    require_write,
)
from ..integrations.crypto import encrypt_json
from ..models import MailboxConnection, Org, SeedInbox, record_event, utcnow
from ..security import TokenError, create_access_token, decode_access_token

router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/deliverability", tags=["deliverability"]
)


class SeedIn(BaseModel):
    address: str = Field(min_length=3, max_length=255)
    provider_label: Literal["gmail", "yahoo", "other"] = "other"
    password: str = Field(min_length=1, max_length=500)
    imap_host: str = ""
    imap_port: int = Field(default=993, ge=1, le=65535)
    username: str = ""


class SeedPatch(BaseModel):
    status: Literal["active", "paused"]


class SettingsPatch(BaseModel):
    seed_test_interval_hours: int


def _org(db: Session, principal: Principal) -> Org:
    org = db.get(Org, principal.org_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Org not found")
    return org


def _seed_or_404(db: Session, seed_id: str, principal: Principal) -> SeedInbox:
    seed = db.get(SeedInbox, seed_id)
    if seed is None or seed.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Seed not found")
    return seed


@router.get("")
def overview(principal: Principal = Depends(get_principal), db: Session = Depends(get_db)) -> dict:
    body = build_overview(db, _org(db, principal), utcnow())
    body["oauth"] = {
        name: {
            "configured": seed_oauth.is_configured(name),
            "credential_vars": meta.credential_vars,
            "redirect_uri": seed_oauth.redirect_uri(name),
        }
        for name, meta in seed_oauth.PROVIDERS.items()
    }
    return body


@router.post("/seeds/oauth/{provider}/connect")
def start_seed_oauth(provider: str, principal: Principal = Depends(require_write)) -> dict:
    """Begin a Google / Yahoo sign-in for a seed (no app password needed)."""
    if provider not in seed_oauth.PROVIDERS:
        raise HTTPException(status_code=404, detail=f"Unknown seed provider {provider!r}")
    if not seed_oauth.is_configured(provider):
        raise HTTPException(
            status_code=503,
            detail=f"{seed_oauth.PROVIDERS[provider].label} sign-in is not configured; "
            f"set {seed_oauth.PROVIDERS[provider].credential_vars}",
        )
    if not principal.user_id:
        raise HTTPException(
            status_code=403, detail="Connect a seed from a logged-in user session, not an API key"
        )
    state = create_access_token(
        subject=principal.user_id,
        org_id=principal.org_id,
        role=OAUTH_STATE_ROLE,
        ttl_seconds=600,
        # "provider" is the callback the state is valid at (Google seeds come
        # back through the Gmail mailbox callback); "purpose" keeps a seed
        # consent from ever becoming a sending mailbox.
        extra={
            "provider": "gmail" if provider == "google" else provider,
            "purpose": "seed",
            "seed_provider": provider,
        },
    )
    return {"auth_url": seed_oauth.authorize_url(provider, state)}


@router.get("/seeds/{seed_provider}/callback", include_in_schema=False)
def seed_oauth_callback(
    seed_provider: str,
    code: str = Query(default=""),
    state: str = Query(default=""),
    error: str = Query(default=""),
    error_description: str = Query(default=""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    from .integrations import _callback_page

    if seed_provider not in seed_oauth.PROVIDERS:
        raise HTTPException(status_code=404, detail="Unknown seed provider")
    if error:
        return _callback_page(
            "Connection cancelled", error_description or "Access was not granted."
        )
    try:
        payload = decode_access_token(state)
        if payload.get("role") != OAUTH_STATE_ROLE:
            raise TokenError("wrong token type")
        if payload.get("purpose") != "seed" or payload.get("seed_provider") != seed_provider:
            raise TokenError("state was not minted for this seed sign-in")
    except TokenError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid state: {exc}") from exc
    _ok, title, body = seed_oauth.complete_connect(
        db, provider=seed_provider, org_id=payload["org"], code=code
    )
    return _callback_page(title, body)


@router.patch("/settings")
def update_settings(
    body: SettingsPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    if body.seed_test_interval_hours not in INTERVALS:
        raise HTTPException(
            status_code=422,
            detail=f"seed_test_interval_hours must be one of {list(INTERVALS)}",
        )
    org = _org(db, principal)
    org.seed_test_interval_hours = body.seed_test_interval_hours
    return {"seed_test_interval_hours": org.seed_test_interval_hours}


@router.post("/seeds", status_code=status.HTTP_201_CREATED)
def add_seed(
    body: SeedIn,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    address = body.address.strip().lower()
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        raise HTTPException(status_code=422, detail="That does not look like an email address")
    if body.provider_label in IMAP_PRESETS:
        host, port = IMAP_PRESETS[body.provider_label]
        # Gmail and Yahoo show app passwords in groups of four; the spaces are
        # display only and are not part of the password.
        password = "".join(body.password.split())
    else:
        host, port = body.imap_host.strip(), body.imap_port
        password = body.password
        if not host:
            raise HTTPException(status_code=422, detail="An IMAP host is needed for this provider")
    existing = db.scalar(
        select(SeedInbox).where(SeedInbox.org_id == principal.org_id, SeedInbox.address == address)
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"{address} is already a seed inbox")
    if db.scalar(
        select(MailboxConnection.id).where(
            MailboxConnection.org_id == principal.org_id,
            MailboxConnection.address == address,
            # A disconnected sending mailbox is just an inbox now.
            MailboxConnection.status != "revoked",
        )
    ):
        raise HTTPException(
            status_code=409,
            detail=f"{address} is a sending mailbox; a seed has to be an inbox you only receive on",
        )

    seed = SeedInbox(
        org_id=principal.org_id,
        address=address,
        kind="imap",
        provider_label=body.provider_label,
        imap_host=host,
        imap_port=port,
        username=(body.username or address).strip(),
        secret_blob=encrypt_json({"password": password}),
    )
    # Sign in before saving: a seed that cannot be read is a form error now,
    # not a row that fails quietly in the background forever.
    try:
        with seeds_module.reader_for(seed) as reader:
            seed.spam_visible = bool(reader.spam_visible)
    except SeedReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    seed.last_checked_at = utcnow()
    db.add(seed)
    db.flush()
    record_event(
        db,
        org_id=principal.org_id,
        type="deliverability.seed_added",
        subject_type="seed_inbox",
        subject_id=seed.id,
        payload={"address": address, "provider": body.provider_label},
    )
    return seed_out(seed)


@router.patch("/seeds/{seed_id}")
def update_seed(
    seed_id: str,
    body: SeedPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    seed = _seed_or_404(db, seed_id, principal)
    seed.status = body.status
    if body.status == "active":
        seed.error = ""
    db.flush()
    return seed_out(seed)


@router.post("/seeds/{seed_id}/check")
def check_seed_now(
    seed_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    seed = _seed_or_404(db, seed_id, principal)
    result = check_seed(db, seed, utcnow())
    return {"seed": seed_out(seed), "result": result}


@router.delete("/seeds/{seed_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_seed(
    seed_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> Response:
    seed = _seed_or_404(db, seed_id, principal)
    db.delete(seed)
    db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/mailboxes/{mailbox_id}/test")
def test_mailbox_now(
    mailbox_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    connection = db.get(MailboxConnection, mailbox_id)
    if connection is None or connection.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found")
    return queue_test_now(db, connection, utcnow())


@router.post("/domains/recheck")
def recheck_domains(
    principal: Principal = Depends(require_write), db: Session = Depends(get_db)
) -> dict:
    now = utcnow()
    checked = []
    for domain in custom_domains(db, principal.org_id):
        refresh_domain(db, principal.org_id, domain, now)
        checked.append(domain)
    return {"checked": checked}

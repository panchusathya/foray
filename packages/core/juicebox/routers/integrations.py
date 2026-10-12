"""Connect external mailboxes (Microsoft Graph / Outlook, Zoho Mail, Gmail).

The callback is unauthenticated by necessity — the browser arrives from the
provider's login page — so it carries a short-lived signed state token minted
at connect time instead of a bearer header. The state also names the provider,
so one callback handler serves all of them.

Callback paths stay per-provider and stable (``/v1/integrations/zoho/callback``)
because they are registered with the provider: changing one means editing an
app registration, so they are treated as fixed API surface.
"""

from __future__ import annotations

import html
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import (
    OAUTH_STATE_ROLE,
    CommittingRoute,
    Principal,
    get_db,
    get_principal,
    require_write,
)
from ..integrations.crypto import decrypt_json, encrypt_json
from ..integrations.providers import (
    ProviderError,
    get_provider,
    provider_names,
)
from ..models import MailboxConnection, new_id, record_event, utcnow
from ..outreach.rotation import sent_in_window
from ..schemas import (
    MailboxConnectionOut,
    MailboxConnectOut,
    MailboxIntegrationOut,
    MailboxProviderOut,
    MailboxSendingPatch,
)
from ..security import TokenError, create_access_token, decode_access_token

logger = logging.getLogger(__name__)

router = APIRouter(
    route_class=CommittingRoute, prefix="/v1/integrations", tags=["integrations"]
)

# Lives in deps because get_principal has to know which roles are single-purpose
# in order to refuse them as bearer tokens; two copies of the string would drift.
_STATE_ROLE = OAUTH_STATE_ROLE
_STATE_TTL_SECONDS = 600
# Env var names per provider, for the "not configured" message. Naming them
# beats a generic error: the fix is always "set these two".
_CREDENTIAL_VARS = {
    "microsoft": "MS_CLIENT_ID and MS_CLIENT_SECRET",
    "zoho": "ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET",
    "gmail": "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET",
}


def _provider_or_404(name: str):
    """Resolve a provider name from the URL.

    An unknown name is a client error, never a silent fallback to Microsoft:
    that would connect the wrong kind of mailbox and send from it.
    """
    try:
        return get_provider(name)
    except ProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc


def _not_configured(name: str) -> HTTPException:
    hint = _CREDENTIAL_VARS.get(name, "its client id and secret")
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"{name} integration is not configured; set {hint}",
    )


def _connection_or_404(
    db: Session, connection_id: str, principal: Principal
) -> MailboxConnection:
    connection = db.get(MailboxConnection, connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    return connection


def _require_active(connection: MailboxConnection) -> None:
    if connection.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Mailbox is {connection.status}; reconnect it first",
        )


@router.get("/mailboxes", response_model=MailboxIntegrationOut)
def list_mailboxes(
    principal: Principal = Depends(get_principal), db: Session = Depends(get_db)
) -> MailboxIntegrationOut:
    """Every connected mailbox in the org, plus which providers can be added.

    Mailboxes are a tenant-wide pool: anybody in the org can send from any of
    them, so this is deliberately not filtered by user.
    """
    now = utcnow()
    rows = db.scalars(
        select(MailboxConnection)
        .where(MailboxConnection.org_id == principal.org_id)
        .order_by(MailboxConnection.created_at.desc())
    ).all()
    connections = []
    for row in rows:
        out = MailboxConnectionOut.model_validate(row)
        out.sent_last_24h = sent_in_window(db, mailbox_id=row.id, now=now)
        connections.append(out)
    providers = []
    for name in provider_names():
        provider = get_provider(name)
        providers.append(
            MailboxProviderOut(
                name=name,
                configured=provider.is_configured(),
                redirect_uri=provider.redirect_uri(),
            )
        )
    return MailboxIntegrationOut(providers=providers, connections=connections)


@router.post("/{provider_name}/connect", response_model=MailboxConnectOut)
def start_connect(
    provider_name: str, principal: Principal = Depends(require_write)
) -> MailboxConnectOut:
    provider = _provider_or_404(provider_name)
    if not provider.is_configured():
        raise _not_configured(provider_name)
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
        extra={"provider": provider_name},
    )
    return MailboxConnectOut(auth_url=provider.authorize_url(state))


def _callback_page(title: str, body: str) -> HTMLResponse:
    """Render the post-consent page.

    Both halves are escaped: ``body`` carries provider text that arrives on the
    query string of an endpoint no login guards (the ``error`` branch answers
    before ``state`` is ever checked), and nginx proxies this API from the same
    origin that serves the app and holds its token in ``localStorage``. Raw
    interpolation here is a session-stealing injection, not a cosmetic bug.
    """
    return HTMLResponse(
        "<!doctype html><html><body style='font-family:system-ui;background:#080D0B;"
        "color:#EAF0EC;display:grid;place-items:center;height:100vh;margin:0'>"
        f"<div style='text-align:center'><h2>{html.escape(title)}</h2>"
        f"<p>{html.escape(body)}</p></div>"
        "</body></html>",
        headers={
            # Nothing on this page needs script or an external fetch.
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
        },
    )


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
        logger.warning("%s consent failed: %s %s", provider_name, error, error_description)
        return _callback_page(
            "Connection cancelled",
            error_description or f"{provider_name} did not grant access.",
        )
    provider = _provider_or_404(provider_name)
    if not provider.is_configured():
        raise _not_configured(provider_name)

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

    org_id, user_id = payload["org"], payload["sub"]
    if payload.get("purpose") == "seed":
        # A seed inbox signing in through the Gmail callback Google already
        # knows about. Branch before the mailbox exchange: that one insists on
        # send scopes a read-only seed never asks for, and must never create a
        # sending mailbox out of a seed consent.
        from ..deliverability.seed_oauth import complete_connect

        if provider_name != "gmail" or payload.get("seed_provider") != "google":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid state")
        _ok, title, body = complete_connect(db, provider="google", org_id=org_id, code=code)
        return _callback_page(title, body)
    try:
        # The whole query string goes through: Zoho reports the data centre
        # this mailbox belongs to as an extra callback param, and the token
        # exchange has to be aimed at that host.
        result = provider.exchange_code(code, dict(request.query_params))
    except ProviderError as exc:
        logger.error("%s connect failed: %s", provider_name, exc)
        return _callback_page("Connection failed", str(exc))

    if not result.address:
        return _callback_page(
            "Connection failed", f"{provider_name} returned no mailbox address."
        )

    connection = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.address == result.address,
        )
    )
    if connection is None:
        connection = MailboxConnection(org_id=org_id, address=result.address)
        db.add(connection)
    connection.provider = provider_name
    connection.user_id = user_id
    connection.display_name = result.display_name
    connection.provider_account_id = result.provider_account_id
    connection.api_base = result.api_base
    connection.auth_base = result.auth_base
    if result.sync_state:
        connection.sync_state = result.sync_state
    connection.token_blob = encrypt_json(_carry_refresh_token(connection, result.tokens))
    connection.status = "active"
    connection.error = ""
    db.flush()
    # Baseline the sync cursors NOW: a reply that lands between connect and the
    # first worker sync must not fall on the wrong side of the cursor.
    _baseline_cursors(db, connection, provider)
    record_event(
        db,
        org_id=org_id,
        type="integration.mailbox_connected",
        subject_type="mailbox",
        subject_id=connection.id,
        payload={"address": connection.address, "provider": provider_name},
    )
    return _callback_page(
        "Mailbox connected",
        f"{connection.address} can now send outreach. You can close this tab.",
    )


def _carry_refresh_token(connection: MailboxConnection, tokens: dict) -> dict:
    """Keep the stored refresh token when a re-consent does not return one.

    Neither provider guarantees a refresh token on every consent: Zoho only
    mints one on the first consent for a client, and Graph omits it when it
    reuses a still-valid grant. Writing the empty string over a working one
    leaves a connection that looks active and dies at the next refresh, so
    reconnecting a mailbox carries the old token forward exactly as the
    refresh path in each client already does.

    An unreadable blob is not an error here: the key has rotated past every
    configured secret, and reconnecting is the fix for that -- the fresh
    tokens win.
    """
    if tokens.get("refresh_token") or not connection.token_blob:
        return tokens
    try:
        stored = decrypt_json(connection.token_blob)
    except ValueError:
        return tokens
    if not stored.get("refresh_token"):
        return tokens
    return {**tokens, "refresh_token": stored["refresh_token"]}


def _baseline_cursors(db: Session, connection: MailboxConnection, provider) -> None:
    from ..integrations.providers import FOLDER_INBOX, FOLDER_SENT

    for folder in (FOLDER_INBOX, FOLDER_SENT):
        try:
            if provider.read_cursor(connection, folder):
                continue
            page = provider.iter_folder(db, connection, folder, cursor="")
            provider.commit_cursor(db, connection, folder, page.next_cursor)
        except Exception as exc:  # noqa: BLE001 - the first sync will baseline instead
            logger.warning(
                "cursor baseline at connect failed for %s/%s: %s",
                connection.address,
                folder,
                exc,
            )
            return
    db.flush()


@router.post("/mailboxes/{connection_id}/sync")
def sync_now(
    connection_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Run one sync pass immediately (the worker also does this on a timer)."""
    connection = _connection_or_404(db, connection_id, principal)
    _require_active(connection)

    from ..integrations.sync import sync_mailbox

    try:
        summary = sync_mailbox(db, connection)
    except ProviderError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return {
        "initialized": summary.initialized,
        "seen": summary.seen,
        "matched": summary.matched,
        "skipped": summary.skipped,
        "intents": summary.intents,
        "drafts_sent": summary.drafts_sent,
        "drafts_discarded": summary.drafts_discarded,
        "sent_ingested": summary.sent_ingested,
        "inbound_ingested": summary.inbound_ingested,
        "last_synced_at": connection.last_synced_at,
    }


@router.patch("/mailboxes/{connection_id}/thread-sync")
def set_thread_sync(
    connection_id: str,
    enabled: bool = Query(...),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Opt in/out of copying candidate correspondence into the CRM timeline.

    Off by default on every connection: connecting a mailbox for sending must
    not silently start retaining its contents. Only mail that resolves to a
    candidate already in a project, sequence or prior thread is ever stored.
    """
    connection = _connection_or_404(db, connection_id, principal)
    connection.thread_sync_enabled = enabled
    db.flush()
    record_event(
        db,
        org_id=principal.org_id,
        type="integration.thread_sync",
        subject_type="mailbox",
        subject_id=connection.id,
        actor_id=principal.user_id,
        payload={"enabled": enabled},
    )
    return {"thread_sync_enabled": connection.thread_sync_enabled}


@router.patch("/mailboxes/{connection_id}/sending", response_model=MailboxConnectionOut)
def set_sending(
    connection_id: str,
    payload: MailboxSendingPatch,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> MailboxConnectionOut:
    """Rotation controls: whether this mailbox sends, and how much per day."""
    connection = _connection_or_404(db, connection_id, principal)
    if payload.send_enabled is not None:
        connection.send_enabled = payload.send_enabled
    if payload.daily_send_cap is not None:
        connection.daily_send_cap = payload.daily_send_cap
    db.flush()
    record_event(
        db,
        org_id=principal.org_id,
        type="integration.mailbox_sending",
        subject_type="mailbox",
        subject_id=connection.id,
        actor_id=principal.user_id,
        payload={
            "send_enabled": connection.send_enabled,
            "daily_send_cap": connection.daily_send_cap,
        },
    )
    out = MailboxConnectionOut.model_validate(connection)
    out.sent_last_24h = sent_in_window(db, mailbox_id=connection.id, now=utcnow())
    return out


def _import_scope(db: Session, *, org_id: str) -> int:
    """How many people the import will look mail up for.

    Shown up front so "imported 0" can be told apart from "there was nobody to
    import for" -- the second is a normal state with a normal explanation, and
    looked identical to a broken button.
    """
    from ..integrations.matching import build_address_index

    return len(build_address_index(db, org_id=org_id))


@router.post("/mailboxes/{connection_id}/backfill")
def backfill_history(
    connection_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Queue the bounded historical import (last MAILBOX_BACKFILL_DAYS days,
    known candidates only). Explicit button, never automatic."""
    from ..jobs import KIND_MAILBOX_BACKFILL, OPEN_STATUSES, enqueue
    from ..models import Job

    connection = _connection_or_404(db, connection_id, principal)
    _require_active(connection)
    if not connection.thread_sync_enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Enable thread sync first — backfill stores mailbox content",
        )
    # Refuse only when a job genuinely exists, rather than trusting the state
    # flag: a worker that died mid-run leaves the flag on "running" forever,
    # and a stuck flag that can never be cleared is worse than a double run.
    in_flight = db.scalar(
        select(Job.id).where(
            Job.org_id == principal.org_id,
            Job.kind == KIND_MAILBOX_BACKFILL,
            Job.idempotency_key.like(f"{KIND_MAILBOX_BACKFILL}:{connection.id}:%"),
            Job.status.in_(OPEN_STATUSES),
        )
    )
    if in_flight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An import is already running for this mailbox",
        )

    now = utcnow()
    # A unique id per run, not a timestamp: two runs inside the same second
    # would mint the same stamp and collide on the idempotency key, which is
    # the very bug this replaces. A fixed key made every import after the
    # first a silent no-op, because enqueue returns the existing row for a key
    # forever -- including one that has already succeeded.
    run = new_id("run")
    scope = _import_scope(db, org_id=principal.org_id)
    connection.backfill_state = {
        "status": "queued",
        "run": run,
        "done": 0,
        "total": scope,
        "imported": 0,
        "note": "" if scope else "no contacts in scope to import for",
        "started_at": now.isoformat(),
        "finished_at": None,
        "error": "",
    }
    job = enqueue(
        db,
        org_id=principal.org_id,
        kind=KIND_MAILBOX_BACKFILL,
        idempotency_key=f"{KIND_MAILBOX_BACKFILL}:{connection.id}:{run}:0",
        payload={
            "org_id": principal.org_id,
            "mailbox_id": connection.id,
            "offset": 0,
            "run": run,
        },
    )
    return {"queued": True, "job_id": job.id, "contacts": scope}


@router.post("/discover")
def start_discovery(
    mailbox_id: str | None = Query(default=None),
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Sweep Sent Items for people who are not in Contacts yet.

    Reads addresses, dates and counts -- never bodies or subjects. Gated on
    thread sync because that is already the org's "yes, read my mailbox"
    switch, and because promotion's follow-up import needs it: without it the
    tray would offer history it can never actually fetch.
    """
    from ..jobs import KIND_CRM_DISCOVERY, enqueue

    eligible = db.scalars(
        select(MailboxConnection).where(
            MailboxConnection.org_id == principal.org_id,
            MailboxConnection.status == "active",
            MailboxConnection.thread_sync_enabled.is_(True),
        )
    ).all()
    if mailbox_id:
        eligible = [c for c in eligible if c.id == mailbox_id]
    if not eligible:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Enable thread sync on a connected mailbox first",
        )

    now = utcnow()
    job = enqueue(
        db,
        org_id=principal.org_id,
        kind=KIND_CRM_DISCOVERY,
        # Timestamped: enqueue returns the existing row for a key forever, so a
        # fixed key would make every sweep after the first a silent no-op.
        idempotency_key=f"{KIND_CRM_DISCOVERY}:{principal.org_id}:{int(now.timestamp())}",
        payload={"org_id": principal.org_id, "mailbox_id": mailbox_id or ""},
    )
    return {"queued": True, "job_id": job.id, "mailboxes": len(eligible)}


@router.post("/mailboxes/{connection_id}/discover")
def start_discovery_for_mailbox(
    connection_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    connection = _connection_or_404(db, connection_id, principal)
    _require_active(connection)
    return start_discovery(mailbox_id=connection.id, principal=principal, db=db)


@router.get("/mailbox-activity")
def mailbox_activity(
    principal: Principal = Depends(get_principal),
    db: Session = Depends(get_db),
) -> list[dict]:
    """What each mailbox is doing: the history import and the contact sweep.

    Every connected mailbox is listed even when idle, so the UI can say "not
    started" rather than rendering nothing and leaving the user guessing.

    Real counters rather than a percentage for the sweep: the total size of a
    mailbox is not known up front, so a percentage would be invented. The
    import does know its denominator -- the address book it walks -- so that
    one is reported as done/total.
    """
    rows = db.scalars(
        select(MailboxConnection)
        .where(
            MailboxConnection.org_id == principal.org_id,
            MailboxConnection.status != "revoked",
        )
        .order_by(MailboxConnection.address)
    ).all()

    out = []
    for connection in rows:
        sweep = connection.discovery_state or {}
        imported = connection.backfill_state or {}
        out.append(
            {
                "mailbox_id": connection.id,
                "address": connection.address,
                "thread_sync_enabled": connection.thread_sync_enabled,
                "discovery": {
                    "status": sweep.get("status", "idle"),
                    "scanned": int(sweep.get("scanned") or 0),
                    "found": int(sweep.get("found") or 0),
                    "started_at": sweep.get("started_at"),
                    "finished_at": sweep.get("finished_at"),
                    "error": sweep.get("error", ""),
                },
                "backfill": {
                    "status": imported.get("status", "idle"),
                    "done": int(imported.get("done") or 0),
                    "total": int(imported.get("total") or 0),
                    "imported": int(imported.get("imported") or 0),
                    "note": imported.get("note", ""),
                    "started_at": imported.get("started_at"),
                    "finished_at": imported.get("finished_at"),
                    "error": imported.get("error", ""),
                },
            }
        )
    return out


@router.delete("/mailboxes/{connection_id}")
def disconnect(
    connection_id: str,
    principal: Principal = Depends(require_write),
    db: Session = Depends(get_db),
) -> dict:
    """Revoke rather than delete.

    Enrollments pin themselves to the mailbox that first emailed a candidate,
    so deleting the row would either break those threads or be refused by the
    foreign key. A revoked mailbox drops out of the rotation and any enrollment
    still pinned to it fails loudly instead of silently moving address.
    """
    connection = _connection_or_404(db, connection_id, principal)
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

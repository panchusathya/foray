"""Microsoft Graph mail: OAuth, token storage, and sendMail.

Delegated send (a recruiter connecting their Outlook mailbox) is the default
path. App-only send is also wired: set ``GRAPH_MAILBOX`` to a UPN and grant
the application ``Mail.Send`` — useful when every sequence should leave from
one shared mailbox without a browser consent.

Nothing here is selected unless ``EMAIL_PROVIDER`` is ``graph`` / ``outlook``.
Console, SMTP, and the in-memory test mailbox stay as they were.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol, TypeVar
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope
from ..models import MailboxConnection, utcnow
from ..security import TokenError, decrypt_secret, encrypt_secret
from .sender import OutboundEmail, SendResult

logger = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
LOGIN_ROOT = "https://login.microsoftonline.com"
DELEGATED_SCOPES = ("offline_access", "User.Read", "Mail.Send")
CLIENT_CREDENTIALS_SCOPE = "https://graph.microsoft.com/.default"
_REFRESH_SKEW = timedelta(seconds=60)


class GraphError(RuntimeError):
    """A Graph or identity-platform failure the sender can surface on a Message."""


@dataclass
class GraphResponse:
    status_code: int
    json_body: Any = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.json_body if self.json_body is not None else {}


class GraphTransport(Protocol):
    name: str

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
    ) -> GraphResponse:  # pragma: no cover - protocol
        ...


class HttpGraphTransport:
    """Real httpx calls. Created lazily so tests never open a client."""

    name = "httpx"

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout
        self._client: httpx.Client | None = None

    def _client_obj(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
    ) -> GraphResponse:
        response = self._client_obj().request(method, url, headers=headers, json=json, data=data)
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return GraphResponse(status_code=response.status_code, json_body=body, text=response.text)


class FakeGraphTransport:
    """Scripted Graph/identity responses. Same idea as ``FakeTransport`` for the LLM.

    ``handlers`` maps a substring of the URL (optionally prefixed with
    ``METHOD ``) to a ``GraphResponse``, exception, or callable of the request.
    Unmatched calls fall through to ``default`` or raise.
    """

    name = "fake"

    def __init__(
        self,
        handlers: dict[str, Any] | None = None,
        *,
        default: Any = None,
    ) -> None:
        self.handlers = dict(handlers or {})
        self._default = default
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def connected(cls, **overrides: Any) -> FakeGraphTransport:
        """A mailbox that can consent and send."""
        handlers: dict[str, Any] = {
            "POST /token": GraphResponse(
                200,
                {
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                    "scope": " ".join(DELEGATED_SCOPES),
                },
            ),
            "GET /me": GraphResponse(
                200,
                {
                    "id": "user-1",
                    "displayName": "Ada Recruiter",
                    "mail": "ada@contoso.com",
                    "userPrincipalName": "ada@contoso.com",
                },
            ),
            "POST /sendMail": GraphResponse(202, None),
        }
        handlers.update(overrides)
        return cls(handlers)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
    ) -> GraphResponse:
        recorded = {
            "method": method.upper(),
            "url": url,
            "headers": dict(headers or {}),
            "json": json,
            "data": dict(data or {}),
        }
        self.calls.append(recorded)
        item = self._resolve(method, url)
        if isinstance(item, BaseException):
            raise item
        if callable(item):
            item = item(recorded)
        if isinstance(item, GraphResponse):
            return item
        raise GraphError(f"FakeGraphTransport has no response for {method} {url}")

    def _resolve(self, method: str, url: str) -> Any:
        method_u = method.upper()
        keyed = f"{method_u} {url}"
        for key, value in self.handlers.items():
            if " " in key:
                want_method, needle = key.split(" ", 1)
                if want_method.upper() == method_u and needle in url:
                    return value
            elif key in url or key in keyed:
                return value
        if self._default is not None:
            return self._default
        raise GraphError(f"FakeGraphTransport has no scripted response for {method} {url}")


T = TypeVar("T")
_transport: GraphTransport | None = None
_bound_session: ContextVar[Session | None] = ContextVar("graph_mailbox_session", default=None)


@contextmanager
def bind_mailbox_session(session: Session) -> Iterator[Session]:
    """Reuse the caller's transaction so SQLite is not asked to write twice."""
    token = _bound_session.set(session)
    try:
        yield session
    finally:
        _bound_session.reset(token)


def _run_with_db(fn: Callable[[Session], T]) -> T:
    existing = _bound_session.get()
    if existing is not None:
        return fn(existing)
    with session_scope() as db:
        return fn(db)


def get_graph_transport() -> GraphTransport:
    global _transport
    if _transport is None:
        _transport = HttpGraphTransport()
    return _transport


def set_graph_transport(transport: GraphTransport | None) -> None:
    """Tests swap in ``FakeGraphTransport`` the same way they swap the LLM."""
    global _transport
    _transport = transport


def graph_error_message(response: GraphResponse) -> str:
    body = response.json() if isinstance(response.json(), dict) else {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        message = err.get("message") or err.get("code") or ""
        if message:
            return str(message)
    if isinstance(err, str) and err:
        description = body.get("error_description") if isinstance(body, dict) else ""
        return f"{err}: {description}".strip(": ")
    if response.text:
        return response.text[:300]
    return f"graph http {response.status_code}"


def send_mail_payload(email: OutboundEmail) -> dict[str, Any]:
    """Map ``OutboundEmail`` onto Graph's sendMail JSON.

    Graph only accepts custom ``internetMessageHeaders`` whose names start
    with ``x-``. List-Unsubscribe stays in the body footer (CAN-SPAM) and is
    still set as a real header on SMTP.
    """
    headers = [
        {"name": key, "value": value}
        for key, value in email.headers.items()
        if key.lower().startswith("x-")
    ]
    message: dict[str, Any] = {
        "subject": email.subject,
        "body": {
            "contentType": "HTML" if email.html_body else "Text",
            "content": email.html_body or email.text_body,
        },
        "toRecipients": [{"emailAddress": {"address": email.to}}],
    }
    if headers:
        message["internetMessageHeaders"] = headers
    juicebox_id = email.headers.get("X-Juicebox-Message-Id", "")
    if juicebox_id:
        message["internetMessageId"] = f"<{juicebox_id}@juicebox.send>"
    return {"message": message, "saveToSentItems": True}


def authorize_url(*, state: str) -> str:
    settings = get_settings()
    if not settings.graph_configured:
        raise GraphError("Outlook is not configured (set GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET)")
    params = {
        "client_id": settings.graph_client_id,
        "response_type": "code",
        "redirect_uri": settings.resolved_graph_redirect_uri(),
        "response_mode": "query",
        "scope": " ".join(DELEGATED_SCOPES),
        "state": state,
        "prompt": "select_account",
    }
    tenant = settings.graph_tenant_id or "common"
    return f"{LOGIN_ROOT}/{quote(tenant, safe='')}/oauth2/v2.0/authorize?{urlencode(params)}"


def _token_url() -> str:
    tenant = get_settings().graph_tenant_id or "common"
    return f"{LOGIN_ROOT}/{quote(tenant, safe='')}/oauth2/v2.0/token"


def _token_request(transport: GraphTransport, data: dict[str, str]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.graph_configured:
        raise GraphError("Outlook is not configured (set GRAPH_CLIENT_ID and GRAPH_CLIENT_SECRET)")
    body = {
        "client_id": settings.graph_client_id,
        "client_secret": settings.graph_client_secret,
        **data,
    }
    response = transport.request("POST", _token_url(), data=body)
    if not response.ok:
        raise GraphError(graph_error_message(response))
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise GraphError("token response missing access_token")
    return payload


def exchange_code(transport: GraphTransport, code: str) -> dict[str, Any]:
    settings = get_settings()
    return _token_request(
        transport,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": settings.resolved_graph_redirect_uri(),
            "scope": " ".join(DELEGATED_SCOPES),
        },
    )


def refresh_tokens(transport: GraphTransport, refresh_token: str) -> dict[str, Any]:
    return _token_request(
        transport,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(DELEGATED_SCOPES),
        },
    )


def client_credentials_token(transport: GraphTransport) -> dict[str, Any]:
    return _token_request(
        transport,
        {
            "grant_type": "client_credentials",
            "scope": CLIENT_CREDENTIALS_SCOPE,
        },
    )


def fetch_profile(transport: GraphTransport, access_token: str) -> dict[str, Any]:
    response = transport.request(
        "GET",
        f"{GRAPH_ROOT}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if not response.ok:
        raise GraphError(graph_error_message(response))
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def apply_token_payload(connection: MailboxConnection, payload: dict[str, Any]) -> None:
    connection.access_token_enc = encrypt_secret(str(payload.get("access_token") or ""))
    refresh = payload.get("refresh_token")
    if refresh:
        connection.refresh_token_enc = encrypt_secret(str(refresh))
    expires_in = int(payload.get("expires_in") or 3600)
    connection.token_expires_at = utcnow() + timedelta(seconds=max(expires_in - 30, 60))
    if payload.get("scope"):
        connection.scopes = str(payload["scope"])[:400]
    connection.status = "connected"
    connection.last_error = ""


def upsert_connection(
    db: Session,
    *,
    org_id: str,
    user_id: str | None,
    token_payload: dict[str, Any],
    profile: dict[str, Any],
) -> MailboxConnection:
    mailbox = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
    name = str(profile.get("displayName") or "")
    row = db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.provider == "microsoft",
        )
    )
    if row is None:
        row = MailboxConnection(org_id=org_id, provider="microsoft")
        db.add(row)
    row.user_id = user_id
    row.mailbox_email = mailbox
    row.mailbox_name = name
    row.tenant_id = get_settings().graph_tenant_id
    apply_token_payload(row, token_payload)
    db.flush()
    return row


def connection_for(db: Session, org_id: str) -> MailboxConnection | None:
    return db.scalar(
        select(MailboxConnection).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.provider == "microsoft",
        )
    )


def _token_is_fresh(connection: MailboxConnection) -> bool:
    if not connection.access_token_enc or connection.status != "connected":
        return False
    if connection.token_expires_at is None:
        return False
    expires = connection.token_expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=utcnow().tzinfo)
    return expires > utcnow() + _REFRESH_SKEW


def _mark_expired(db: Session, connection: MailboxConnection, error: str) -> None:
    connection.status = "expired"
    connection.last_error = error[:500]
    db.flush()


def delegated_access_token(
    db: Session,
    connection: MailboxConnection,
    transport: GraphTransport,
    *,
    force_refresh: bool = False,
) -> str:
    if not force_refresh and _token_is_fresh(connection):
        try:
            return decrypt_secret(connection.access_token_enc)
        except TokenError as exc:
            raise GraphError("stored Outlook token could not be decrypted") from exc

    if not connection.refresh_token_enc:
        _mark_expired(db, connection, "no refresh token")
        raise GraphError("Outlook mailbox needs to be reconnected")

    try:
        refresh = decrypt_secret(connection.refresh_token_enc)
    except TokenError as exc:
        _mark_expired(db, connection, "token decrypt failed")
        raise GraphError("stored Outlook token could not be decrypted") from exc

    try:
        payload = refresh_tokens(transport, refresh)
    except GraphError as exc:
        _mark_expired(db, connection, str(exc))
        raise GraphError("Outlook mailbox needs to be reconnected") from exc

    apply_token_payload(connection, payload)
    db.flush()
    return decrypt_secret(connection.access_token_enc)


@dataclass
class _SendCreds:
    access_token: str
    send_path: str
    mailbox_email: str


_app_token: tuple[str, float] | None = None


def _client_credentials_creds(
    transport: GraphTransport, *, force_refresh: bool = False
) -> _SendCreds:
    global _app_token
    settings = get_settings()
    mailbox = settings.graph_mailbox.strip()
    if not mailbox:
        raise GraphError("no Outlook mailbox connected for this workspace")
    now = utcnow().timestamp()
    if not force_refresh and _app_token and _app_token[1] > now + 60:
        token = _app_token[0]
    else:
        payload = client_credentials_token(transport)
        expires_in = int(payload.get("expires_in") or 3600)
        token = str(payload["access_token"])
        _app_token = (token, now + max(expires_in - 30, 60))
    path = f"/users/{quote(mailbox, safe='@.')}/sendMail"
    return _SendCreds(access_token=token, send_path=path, mailbox_email=mailbox)


def credentials_for(
    org_id: str,
    transport: GraphTransport,
    *,
    force_refresh: bool = False,
) -> _SendCreds:
    """Delegated connection first; env ``GRAPH_MAILBOX`` as the app-only fallback."""

    def _from_db(db: Session) -> _SendCreds | None:
        if not org_id:
            return None
        connection = connection_for(db, org_id)
        if connection is None:
            return None
        token = delegated_access_token(db, connection, transport, force_refresh=force_refresh)
        return _SendCreds(
            access_token=token,
            send_path="/me/sendMail",
            mailbox_email=connection.mailbox_email,
        )

    creds = _run_with_db(_from_db)
    if creds is not None:
        return creds
    return _client_credentials_creds(transport, force_refresh=force_refresh)


def reset_app_token_cache() -> None:
    global _app_token
    _app_token = None


class GraphSender:
    """``EmailSender`` that delivers through Microsoft Graph sendMail."""

    name = "graph"

    def __init__(self, transport: GraphTransport | None = None) -> None:
        self._transport = transport

    @property
    def transport(self) -> GraphTransport:
        return self._transport or get_graph_transport()

    def send(self, email: OutboundEmail) -> SendResult:
        try:
            creds = credentials_for(email.org_id, self.transport)
            result = self._post_send(email, creds)
            if result is not None:
                return result
            creds = credentials_for(email.org_id, self.transport, force_refresh=True)
            result = self._post_send(email, creds)
            if result is not None:
                return result
            return SendResult(ok=False, error="graph send failed after token refresh")
        except GraphError as exc:
            logger.error("graph send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced on the Message row
            logger.error("graph send failed: %s", exc)
            return SendResult(ok=False, error=str(exc))

    def _post_send(self, email: OutboundEmail, creds: _SendCreds) -> SendResult | None:
        response = self.transport.request(
            "POST",
            f"{GRAPH_ROOT}{creds.send_path}",
            headers={
                "Authorization": f"Bearer {creds.access_token}",
                "Content-Type": "application/json",
            },
            json=send_mail_payload(email),
        )
        if response.status_code == 401:
            return None
        if not response.ok:
            return SendResult(ok=False, error=graph_error_message(response))
        juicebox_id = email.headers.get("X-Juicebox-Message-Id", "")
        provider_id = f"graph:{juicebox_id}" if juicebox_id else f"graph:{uuid.uuid4().hex[:16]}"
        return SendResult(ok=True, provider_message_id=provider_id)

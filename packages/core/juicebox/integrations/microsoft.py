"""Microsoft Graph: OAuth for the recruiter's Outlook mailbox, and sending.

Auth-code flow, hand-rolled over httpx — we store tokens ourselves (encrypted,
see crypto.py) so MSAL's cache machinery buys nothing here. Sends use
draft-then-send instead of /sendMail because a created draft returns stable
ids (immutable id, internetMessageId, conversationId) that reply detection
matches against later.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from ..models import MailboxConnection
from .crypto import decrypt_json, encrypt_json

logger = logging.getLogger(__name__)

LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# offline_access yields the refresh token; the rest are delegated Graph scopes.
SCOPES = "offline_access User.Read Mail.Send Mail.ReadWrite"

_TOKEN_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 30.0


class MicrosoftError(Exception):
    def __init__(self, detail: str, *, status_code: int = 0) -> None:
        super().__init__(detail)
        self.status_code = status_code


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.ms_client_id and settings.ms_client_secret)


def redirect_uri() -> str:
    settings = get_settings()
    if settings.ms_redirect_uri:
        return settings.ms_redirect_uri
    return f"{settings.base_url.rstrip('/')}/v1/integrations/microsoft/callback"


def authorize_url(state: str) -> str:
    settings = get_settings()
    query = urlencode(
        {
            "client_id": settings.ms_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri(),
            "response_mode": "query",
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{LOGIN_BASE}/{settings.ms_tenant_id}/oauth2/v2.0/authorize?{query}"


def _token_request(grant: dict) -> dict:
    """POST to the token endpoint and normalise the result for storage."""
    settings = get_settings()
    data = {
        "client_id": settings.ms_client_id,
        "client_secret": settings.ms_client_secret,
        "scope": SCOPES,
        **grant,
    }
    url = f"{LOGIN_BASE}/{settings.ms_tenant_id}/oauth2/v2.0/token"
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.post(url, data=data)
    if response.status_code >= 400:
        try:
            detail = response.json().get("error_description", response.text)
        except ValueError:
            detail = response.text
        raise MicrosoftError(
            f"token request failed: {detail}", status_code=response.status_code
        )
    payload = response.json()
    return {
        "access_token": payload["access_token"],
        # Microsoft rotates refresh tokens; a missing one means keep the old.
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
    }


def exchange_code(code: str) -> dict:
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}
    )


def refresh_tokens(refresh_token: str) -> dict:
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def fetch_profile(access_token: str) -> dict:
    """GET /me — the connected mailbox's address, name and Graph user id."""
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.get(
            f"{GRAPH_BASE}/me", headers={"Authorization": f"Bearer {access_token}"}
        )
    if response.status_code >= 400:
        raise MicrosoftError(
            f"profile request failed: {response.text}", status_code=response.status_code
        )
    data = response.json()
    return {
        "address": (data.get("mail") or data.get("userPrincipalName") or "").lower(),
        "display_name": data.get("displayName", ""),
        "ms_user_id": data.get("id", ""),
    }


class GraphClient:
    """Authenticated Graph calls for one connected mailbox.

    Refreshes the access token when it is within a minute of expiry and
    persists the new blob; a failed refresh marks the connection ``error`` so
    the UI surfaces it instead of every send crashing the same way.
    """

    def __init__(self, db, connection: MailboxConnection) -> None:
        self.db = db
        self.connection = connection

    def _access_token(self) -> str:
        tokens = decrypt_json(self.connection.token_blob)
        if tokens["expires_at"] - _TOKEN_SKEW_SECONDS > time.time():
            return tokens["access_token"]
        try:
            fresh = refresh_tokens(tokens["refresh_token"])
        except MicrosoftError as exc:
            self.connection.status = "error"
            self.connection.error = str(exc)
            self.db.flush()
            raise
        if not fresh["refresh_token"]:
            fresh["refresh_token"] = tokens["refresh_token"]
        self.connection.token_blob = encrypt_json(fresh)
        self.connection.status = "active"
        self.connection.error = ""
        self.db.flush()
        return fresh["access_token"]

    def _request(
        self, method: str, path: str, *, json: dict | None = None, headers: dict | None = None
    ) -> httpx.Response:
        request_headers = {"Authorization": f"Bearer {self._access_token()}"}
        if headers:
            request_headers.update(headers)
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.request(
                method, f"{GRAPH_BASE}{path}", json=json, headers=request_headers
            )
        if response.status_code >= 400:
            raise MicrosoftError(
                f"graph {method} {path} failed ({response.status_code}): {response.text[:500]}",
                status_code=response.status_code,
            )
        return response

    def create_draft(self, message: dict) -> dict:
        response = self._request(
            "POST",
            "/me/messages",
            json=message,
            headers={"Prefer": 'IdType="ImmutableId"'},
        )
        return response.json()

    def send_draft(self, message_id: str) -> None:
        self._request("POST", f"/me/messages/{message_id}/send")


class GraphSender:
    """EmailSender provider that sends as the connected mailbox.

    The From is always the mailbox itself — Graph will not spoof a sequence's
    from_email, which is exactly the point of the integration.
    """

    name = "graph"

    def __init__(self, db, connection: MailboxConnection, client: GraphClient | None = None):
        self.connection = connection
        self.client = client or GraphClient(db, connection)

    def send(self, email) -> SendResult:  # noqa: F821 - see import below
        from ..outreach.sender import SendResult

        message = {
            "subject": email.subject,
            "body": {"contentType": "HTML", "content": email.html_body or email.text_body},
            "toRecipients": [{"emailAddress": {"address": email.to}}],
        }
        if email.headers:
            # Graph only accepts custom x-headers here; ours all qualify.
            message["internetMessageHeaders"] = [
                {"name": name, "value": value}
                for name, value in email.headers.items()
                if name.lower().startswith("x-")
            ]
        try:
            draft = self.client.create_draft(message)
            self.client.send_draft(draft["id"])
        except (MicrosoftError, ValueError) as exc:
            logger.error("graph send failed for %s: %s", self.connection.address, exc)
            return SendResult(ok=False, error=str(exc))
        return SendResult(
            ok=True,
            provider_message_id=draft.get("internetMessageId", "") or draft.get("id", ""),
            graph_message_id=draft.get("id", ""),
            internet_message_id=draft.get("internetMessageId", ""),
            conversation_id=draft.get("conversationId", ""),
        )

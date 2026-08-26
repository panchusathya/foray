"""Outlook, read-only, for the candidate's own resume.

Deliberately self-contained rather than parameterising
``integrations/microsoft.py``: that module's scopes include ``Mail.Send`` and
its tenant defaults to ``organizations`` (work accounts only). Threading
optional kwargs through it would put a send-capable scope one default away
from a candidate token and reject the personal outlook.com accounts
candidates actually have. The ~40 lines of token normalisation are copied
instead — the price of the two mistakes being unrepresentable.

Separate app registration: ``signInAudience`` must allow personal Microsoft
accounts, and the tenant path here defaults to ``common`` for that reason.
Microsoft has no token-revocation endpoint; disconnect wipes our copy and the
person can remove the app under account.microsoft.com consents.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .base import ConnectorError, ConnectorGrant, ConnectorProfile, ResumeHit, fresh_access_token

LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
#: Read-only, and it must stay that way — see the module docstring.
SCOPES = "offline_access User.Read Mail.Read"

_HTTP_TIMEOUT = 30.0
_MAX_MESSAGES_PER_FOLDER = 15
_RESUME_MIMES = (
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
)


def _fail(response: httpx.Response, doing: str) -> ConnectorError:
    try:
        payload = response.json()
        detail = payload.get("error_description") or (payload.get("error") or {}).get(
            "message", ""
        )
    except (ValueError, AttributeError):
        detail = response.text
    return ConnectorError(f"{doing} failed: {detail}", status_code=response.status_code)


class MicrosoftCandidateConnector:
    name = "microsoft"
    label = "Outlook"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.ms_candidate_client_id and settings.ms_candidate_client_secret)

    def redirect_uri(self) -> str:
        settings = get_settings()
        if settings.ms_candidate_redirect_uri:
            return settings.ms_candidate_redirect_uri
        return f"{settings.base_url.rstrip('/')}/v1/connect/microsoft/callback"

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        query = urlencode(
            {
                "client_id": settings.ms_candidate_client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri(),
                "response_mode": "query",
                "scope": SCOPES,
                "state": state,
            }
        )
        tenant = settings.ms_candidate_tenant_id
        return f"{LOGIN_BASE}/{tenant}/oauth2/v2.0/authorize?{query}"

    def _token_request(self, grant: dict) -> dict:
        settings = get_settings()
        data = {
            "client_id": settings.ms_candidate_client_id,
            "client_secret": settings.ms_candidate_client_secret,
            "scope": SCOPES,
            **grant,
        }
        url = f"{LOGIN_BASE}/{settings.ms_candidate_tenant_id}/oauth2/v2.0/token"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.post(url, data=data)
        if response.status_code >= 400:
            raise _fail(response, "token request")
        payload = response.json()
        return {
            "access_token": payload["access_token"],
            # Microsoft rotates refresh tokens; a missing one means keep the old.
            "refresh_token": payload.get("refresh_token", ""),
            "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
        }

    def refresh_tokens(self, refresh_token: str) -> dict:
        if not refresh_token:
            raise ConnectorError("no refresh token stored; reconnect required")
        return self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def exchange_code(self, code: str, params: Mapping[str, str]) -> ConnectorGrant:
        tokens = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri(),
            }
        )
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.get(
                f"{GRAPH_BASE}/me",
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
        if response.status_code >= 400:
            raise _fail(response, "profile request")
        data = response.json()
        address = str(data.get("mail") or data.get("userPrincipalName") or "").lower()
        if not address:
            raise ConnectorError("microsoft returned no mailbox address")
        return ConnectorGrant(
            tokens=tokens,
            address=address,
            display_name=str(data.get("displayName") or ""),
            provider_account_id=str(data.get("id") or ""),
            scopes=SCOPES,
        )

    # -- reads ------------------------------------------------------------

    def _get(self, db, connection, path: str, params: dict | None = None) -> httpx.Response:
        token = fresh_access_token(db, connection, refresh=self.refresh_tokens)
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.get(
                f"{GRAPH_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise _fail(response, f"graph GET {path}")
        return response

    def list_resume_hits(self, db, connection) -> list[ResumeHit]:
        hits: list[ResumeHit] = []
        # Sent first: a file they attached themselves is the resume they use.
        for folder, sent_by_owner in (("sentitems", True), ("inbox", False)):
            listing = self._get(
                db,
                connection,
                f"/me/mailFolders/{folder}/messages",
                {
                    "$filter": "hasAttachments eq true",
                    "$select": "id,sentDateTime,receivedDateTime",
                    "$top": _MAX_MESSAGES_PER_FOLDER,
                },
            ).json()
            for message in listing.get("value") or []:
                message_id = str(message.get("id") or "")
                if not message_id:
                    continue
                stamp = message.get("sentDateTime") or message.get("receivedDateTime") or ""
                occurred_at = _parse_iso(stamp)
                attachments = self._get(
                    db,
                    connection,
                    f"/me/messages/{message_id}/attachments",
                    {"$select": "id,name,contentType,size,isInline"},
                ).json()
                for attachment in attachments.get("value") or []:
                    if attachment.get("isInline"):
                        continue
                    name = str(attachment.get("name") or "")
                    content_type = str(attachment.get("contentType") or "")
                    # Unknown types still pass on the filename; extract's
                    # extension filter and the byte sniff are the gates.
                    if (
                        content_type
                        and content_type not in _RESUME_MIMES
                        and not name.lower().endswith((".pdf", ".doc", ".docx"))
                    ):
                        continue
                    hits.append(
                        ResumeHit(
                            native_id=f"{message_id}|{attachment.get('id', '')}",
                            filename=name,
                            content_type=content_type,
                            sent_by_owner=sent_by_owner,
                            occurred_at=occurred_at,
                            size_bytes=int(attachment.get("size") or 0),
                        )
                    )
        return hits

    def fetch_resume(self, db, connection, hit: ResumeHit) -> bytes:
        message_id, _, attachment_id = hit.native_id.partition("|")
        if not message_id or not attachment_id:
            raise ConnectorError(f"malformed hit id {hit.native_id!r}")
        response = self._get(
            db, connection, f"/me/messages/{message_id}/attachments/{attachment_id}/$value"
        )
        return response.content

    def fetch_profile(self, db, connection) -> ConnectorProfile:
        return ConnectorProfile(full_name=connection.display_name or "")

    def revoke(self, db, connection) -> None:
        """Microsoft exposes no revocation endpoint; wiping our stored tokens
        is the whole action here (the router does that)."""
        return


def _parse_iso(stamp: str) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None

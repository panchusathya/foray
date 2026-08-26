"""Gmail, read-only, for the candidate's own resume.

Hand-rolled auth-code flow over httpx, the same posture as
``integrations/microsoft.py`` — we store tokens ourselves (encrypted) and the
Google SDK buys nothing but a dependency.

Scope note: ``gmail.readonly`` is a Google *restricted* scope. The narrower
``gmail.metadata`` cannot do this job — it forbids ``q=`` search and
attachment download, which are the only two Gmail calls this module makes.
The mitigation is structural, not verbal: the queries below are the complete
list, the DTOs have no field for a subject or a body, and the only bytes ever
persisted are one attachment the candidate chose to hear back about.
Publishing past Google's Testing mode requires app verification plus a CASA
assessment — an ops decision, tracked in docs/INTAKE_COMPLIANCE.md, that
``is_configured()`` keeps out of the code.

Testing-mode caveat: refresh tokens expire after 7 days there. The first
index runs at callback time, so that limit only bites a later re-index.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .base import ConnectorError, ConnectorGrant, ConnectorProfile, ResumeHit, fresh_access_token

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPES = "openid email profile https://www.googleapis.com/auth/gmail.readonly"

_HTTP_TIMEOUT = 30.0
#: The complete list of Gmail searches this integration runs. Their own
#: attachments first (the strongest resume signal), then resume-named files
#: anywhere. Adding a query here is a privacy-posture change, not a tweak.
RESUME_QUERIES: tuple[tuple[str, bool], ...] = (
    ("in:sent has:attachment (filename:pdf OR filename:docx)", True),
    ("has:attachment (filename:pdf OR filename:docx) (resume OR cv)", False),
)
_MAX_MESSAGES_PER_QUERY = 15


def _fail(response: httpx.Response, doing: str) -> ConnectorError:
    try:
        detail = response.json().get("error_description") or response.json().get("error", "")
        if isinstance(detail, dict):
            detail = detail.get("message", "")
    except ValueError:
        detail = response.text
    return ConnectorError(f"{doing} failed: {detail}", status_code=response.status_code)


class GoogleConnector:
    name = "google"
    label = "Gmail"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.google_connect_client_id and settings.google_connect_client_secret)

    def redirect_uri(self) -> str:
        settings = get_settings()
        if settings.google_connect_redirect_uri:
            return settings.google_connect_redirect_uri
        return f"{settings.base_url.rstrip('/')}/v1/connect/google/callback"

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        query = urlencode(
            {
                "client_id": settings.google_connect_client_id,
                "response_type": "code",
                "redirect_uri": self.redirect_uri(),
                "scope": SCOPES,
                "state": state,
                # offline + consent is what yields a refresh token on every
                # connect, not just the first; granted-scope inheritance is
                # off so a wider earlier grant cannot silently widen this one.
                "access_type": "offline",
                "prompt": "consent",
                "include_granted_scopes": "false",
            }
        )
        return f"{AUTH_URL}?{query}"

    def _token_request(self, grant: dict) -> dict:
        settings = get_settings()
        data = {
            "client_id": settings.google_connect_client_id,
            "client_secret": settings.google_connect_client_secret,
            **grant,
        }
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.post(TOKEN_URL, data=data)
        if response.status_code >= 400:
            raise _fail(response, "token request")
        payload = response.json()
        return {
            "access_token": payload["access_token"],
            # Google omits the refresh token on re-consent sometimes; keep the
            # old one, same convention as the Microsoft provider.
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
                USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
        if response.status_code >= 400:
            raise _fail(response, "userinfo request")
        info = response.json()
        address = str(info.get("email") or "").lower()
        if not address:
            raise ConnectorError("google returned no verified email")
        return ConnectorGrant(
            tokens=tokens,
            address=address,
            display_name=str(info.get("name") or ""),
            provider_account_id=str(info.get("sub") or ""),
            scopes=SCOPES,
        )

    # -- reads ------------------------------------------------------------

    def _get(self, db, connection, path: str, params: dict | None = None) -> dict:
        token = fresh_access_token(db, connection, refresh=self.refresh_tokens)
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.get(
                f"{GMAIL_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise _fail(response, f"gmail GET {path}")
        return response.json()

    def list_resume_hits(self, db, connection) -> list[ResumeHit]:
        hits: list[ResumeHit] = []
        seen: set[str] = set()
        for query, sent_by_owner in RESUME_QUERIES:
            listing = self._get(
                db,
                connection,
                "/messages",
                {"q": query, "maxResults": _MAX_MESSAGES_PER_QUERY},
            )
            for stub in listing.get("messages") or []:
                message_id = str(stub.get("id") or "")
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)
                message = self._get(db, connection, f"/messages/{message_id}", {"format": "full"})
                hits.extend(
                    self._attachment_hits(message, sent_by_owner=sent_by_owner)
                )
        return hits

    @staticmethod
    def _attachment_hits(message: dict, *, sent_by_owner: bool) -> list[ResumeHit]:
        """Walk the part tree for real attachments. Filenames and ids only —
        part bodies exist in the response and are deliberately not read."""
        occurred_at = None
        internal = message.get("internalDate")
        if internal:
            occurred_at = datetime.fromtimestamp(int(internal) / 1000.0, tz=UTC)

        found: list[ResumeHit] = []

        def walk(part: dict) -> None:
            filename = str(part.get("filename") or "")
            body = part.get("body") or {}
            attachment_id = str(body.get("attachmentId") or "")
            if filename and attachment_id:
                found.append(
                    ResumeHit(
                        native_id=f"{message.get('id', '')}:{attachment_id}",
                        filename=filename,
                        content_type=str(part.get("mimeType") or ""),
                        sent_by_owner=sent_by_owner,
                        occurred_at=occurred_at,
                        size_bytes=int(body.get("size") or 0),
                    )
                )
            for child in part.get("parts") or []:
                walk(child)

        payload = message.get("payload") or {}
        if payload:
            walk(payload)
        return found

    def fetch_resume(self, db, connection, hit: ResumeHit) -> bytes:
        message_id, _, attachment_id = hit.native_id.partition(":")
        if not message_id or not attachment_id:
            raise ConnectorError(f"malformed hit id {hit.native_id!r}")
        payload = self._get(
            db, connection, f"/messages/{message_id}/attachments/{attachment_id}"
        )
        data = str(payload.get("data") or "")
        if not data:
            raise ConnectorError("attachment came back empty")
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))

    def fetch_profile(self, db, connection) -> ConnectorProfile:
        """Name only, from the identity the grant verified. Reading bodies or
        signatures for more would break the targeted-extraction posture."""
        return ConnectorProfile(full_name=connection.display_name or "")

    def revoke(self, db, connection) -> None:
        from ..integrations.crypto import decrypt_json

        try:
            tokens = decrypt_json(connection.token_blob)
        except Exception:  # noqa: BLE001 - nothing to revoke is a success
            return
        token = tokens.get("refresh_token") or tokens.get("access_token") or ""
        if not token:
            return
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            http.post(REVOKE_URL, params={"token": token})


def job_search_signal(connector: GoogleConnector, db, connection) -> dict[str, Any]:
    """One count, no content: how actively is this person already applying?

    ``resultSizeEstimate`` of a single targeted query. Stored in
    ``index_state.signals`` as a number — never the messages behind it.
    """
    listing = connector._get(
        db,
        connection,
        "/messages",
        {"q": 'in:sent newer_than:1y (subject:application OR subject:applying)', "maxResults": 1},
    )
    return {"applications_sent_last_year": int(listing.get("resultSizeEstimate") or 0)}

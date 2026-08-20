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
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from ..models import MailboxConnection
from .crypto import decrypt_json, encrypt_json
from .providers import (
    FOLDER_INBOX,
    FOLDER_SENT,
    ConnectResult,
    DraftResult,
    FolderPage,
    MailboxEvent,
    ProviderError,
    SweepPage,
    SweepRecord,
)

logger = logging.getLogger(__name__)

LOGIN_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# offline_access yields the refresh token; the rest are delegated Graph scopes.
SCOPES = "offline_access User.Read Mail.Send Mail.ReadWrite"

_TOKEN_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 30.0


class MicrosoftError(ProviderError):
    """Graph-side failure. A ProviderError so the sync loop can survive one
    broken mailbox without knowing which provider it belongs to."""


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
        "provider_account_id": data.get("id", ""),
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
        url = path if path.startswith("https://") else f"{GRAPH_BASE}{path}"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.request(method, url, json=json, headers=request_headers)
            if response.status_code in (429, 503):
                # Graph throttling. Honour Retry-After once (bounded — the job
                # queue owns real backoff); bulk callers like backfill would
                # otherwise hard-fail on the first throttled page.
                import time as _time

                delay = min(float(response.headers.get("Retry-After") or 2), 30.0)
                _time.sleep(delay)
                response = http.request(method, url, json=json, headers=request_headers)
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

    def update_draft(self, message_id: str, patch: dict) -> dict:
        """Overwrite fields on an unsent draft.

        Used on a createReply draft: Graph fills it with the thread's headers
        and the quoted original, and this replaces the parts that are ours.
        """
        response = self._request(
            "PATCH",
            f"/me/messages/{message_id}",
            json=patch,
            headers={"Prefer": 'IdType="ImmutableId"'},
        )
        return response.json()

    def create_reply_draft(self, message_id: str, comment_html: str) -> dict:
        """Draft a reply on the actual thread; it lands in the user's Drafts.

        ``comment`` sits above the quoted original, exactly like hitting Reply
        in Outlook and typing.
        """
        response = self._request(
            "POST",
            f"/me/messages/{message_id}/createReply",
            json={"comment": comment_html},
            headers={"Prefer": 'IdType="ImmutableId"'},
        )
        return response.json()

    # Fields the inbox sync needs; body arrives as text via the Prefer header.
    _DELTA_SELECT = (
        "$select=id,conversationId,internetMessageId,from,toRecipients,ccRecipients,"
        "subject,bodyPreview,body,isDraft,receivedDateTime,sentDateTime,"
        "hasAttachments,webLink"
    )

    def _delta_baseline(self, folder: str) -> str:
        """Start delta tracking at 'now' — history is not outreach traffic."""
        url = f"/me/mailFolders/{folder}/messages/delta?$deltatoken=latest&{self._DELTA_SELECT}"
        page = self._request("GET", url, headers={"Prefer": 'IdType="ImmutableId"'}).json()
        return page.get("@odata.deltaLink", "")

    def inbox_delta_baseline(self) -> str:
        return self._delta_baseline("inbox")

    def sent_delta_baseline(self) -> str:
        return self._delta_baseline("sentitems")

    def inbox_delta(self, delta_link: str) -> tuple[list[dict], str]:
        """Walk every page since the stored delta link (works for any folder —
        the link itself carries the folder).

        Returns (messages, new_delta_link). The Prefer headers keep ids
        immutable across mailbox moves and bodies in plain text.
        """
        headers = {
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
        }
        messages: list[dict] = []
        url = delta_link
        while url:
            page = self._request("GET", url, headers=headers).json()
            messages.extend(page.get("value", []))
            next_link = page.get("@odata.nextLink")
            if next_link:
                url = next_link
                continue
            return messages, page.get("@odata.deltaLink", delta_link)
        return messages, delta_link

    # Same walker; the name keeps call sites honest about which cursor they hold.
    sent_delta = inbox_delta

    # No body, no bodyPreview, and no subject: the discovery tray has nowhere
    # to put them, so they are never requested. Enforced at the wire rather
    # than by remembering to drop them later.
    _SWEEP_SELECT = (
        "$select=id,conversationId,from,toRecipients,ccRecipients,isDraft,sentDateTime"
    )

    def sent_history(self, since: datetime, *, cursor: str = "", top: int = 100):
        """One page of Sent Items back to ``since``, newest first.

        ``$filter`` and ``$orderby`` must both be on ``sentDateTime`` or Graph
        answers InefficientFilter. ``$top`` above 100 is unreliable on
        /messages. Paging follows ``@odata.nextLink``, which carries its own
        skiptoken and is what the caller persists.
        """
        if cursor:
            url = cursor
        else:
            stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (
                "/me/mailFolders/sentitems/messages"
                f"?$filter=sentDateTime ge {stamp}"
                "&$orderby=sentDateTime desc"
                f"&$top={min(top, 100)}&{self._SWEEP_SELECT}"
            )
        page = self._request(
            "GET", url, headers={"Prefer": 'IdType="ImmutableId"'}
        ).json()
        return page.get("value", []), page.get("@odata.nextLink", "")

    def search_participant(self, address: str, *, top: int = 25) -> list[dict]:
        """Messages exchanged with one address, for the bounded history backfill.

        ``$search`` forbids ``$orderby``/``$filter``, so the date window is the
        caller's job, applied client-side. One page only — this is a per-known-
        candidate lookup, not a mailbox crawl.
        """
        quoted = address.replace('"', "")
        url = (
            f'/me/messages?$search="participants:{quoted}"'
            f"&$top={min(top, 25)}&{self._DELTA_SELECT}"
        )
        headers = {
            "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text"',
        }
        page = self._request("GET", url, headers=headers).json()
        return page.get("value", [])


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

        try:
            draft = self._draft_for(email)
            self.client.send_draft(draft["id"])
        except (MicrosoftError, ValueError) as exc:
            logger.error("graph send failed for %s: %s", self.connection.address, exc)
            return SendResult(ok=False, error=str(exc))
        return SendResult(
            ok=True,
            provider_message_id=draft.get("internetMessageId", "") or draft.get("id", ""),
            provider_native_id=draft.get("id", ""),
            internet_message_id=draft.get("internetMessageId", ""),
            conversation_id=draft.get("conversationId", ""),
        )

    def _draft_for(self, email) -> dict:
        """The draft this send goes out as: a reply on the thread when there is
        one, otherwise a new message.

        Graph takes ``internetMessageHeaders`` for x- names only, so it will not
        accept In-Reply-To or References. A follow-up built as a fresh draft
        therefore arrives as a brand new conversation whatever headers we ask
        for, which is what it was doing: same subject, new thread, and the
        candidate reads it as a second cold email. ``createReply`` is the way to
        put a send on an existing Outlook thread -- Graph copies the
        conversation and the RFC threading headers onto the draft, and this
        overwrites the parts that are ours.
        """
        body = {"contentType": "HTML", "content": email.html_body or email.text_body}
        recipients = [{"emailAddress": {"address": email.to}}]
        headers = [
            {"name": name, "value": value}
            for name, value in (email.headers or {}).items()
            if name.lower().startswith("x-")
        ]

        if email.thread_native_id:
            patch = {
                # createReply addresses the reply to the parent's From, which
                # on a message we sent is this mailbox. Left alone, the
                # follow-up would go to ourselves.
                "toRecipients": recipients,
                "subject": email.subject,
                # Replaces Graph's quoted original rather than appending to it:
                # the quote carries the previous step's tracking pixel, so
                # every open of the follow-up would also count as an open of
                # the email before it.
                "body": body,
            }
            if headers:
                patch["internetMessageHeaders"] = headers
            try:
                reply = self.client.create_reply_draft(email.thread_native_id, "")
                updated = self.client.update_draft(reply["id"], patch) or {}
            except (MicrosoftError, ValueError) as exc:
                # Either the parent is gone (deleted, moved out of this
                # mailbox) or Graph refused the draft. Covers both calls on
                # purpose: an unthreaded send is worse than a threaded one and
                # much better than a step that fails five times and parks.
                logger.warning(
                    "graph reply on %s failed for %s, sending unthreaded: %s",
                    email.thread_native_id,
                    self.connection.address,
                    exc,
                )
            else:
                # PATCH returns the message, but fall back to the createReply
                # response for anything it leaves out -- the id has to be there.
                return {**reply, **updated}

        message = {"subject": email.subject, "body": body, "toRecipients": recipients}
        if headers:
            message["internetMessageHeaders"] = headers
        return self.client.create_draft(message)


def _stamp(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recipients(payload: dict, key: str) -> tuple[tuple[str, str], ...]:
    """(address, display name) pairs. Graph supplies both; keeping only the
    address is what left discovery guessing names from local parts."""
    out = []
    for entry in payload.get(key) or []:
        contact = (entry or {}).get("emailAddress") or {}
        address = contact.get("address", "")
        if address:
            out.append((address.lower(), (contact.get("name") or "").strip()))
    return tuple(out)


def to_sweep_record(payload: dict) -> SweepRecord:
    """A raw Graph message as a discovery record.

    Not routed through ``to_event``: that merges toRecipients and ccRecipients
    into one list and fills ``body`` from ``bodyPreview``. Discovery needs To
    and Cc kept apart, and must never hold a body.
    """
    sender = ((payload.get("from") or {}).get("emailAddress") or {}).get("address", "") or ""
    to = _recipients(payload, "toRecipients")
    cc = _recipients(payload, "ccRecipients")
    return SweepRecord(
        native_id=payload.get("id", "") or "",
        from_address=sender.lower(),
        to_addresses=tuple(address for address, _ in to),
        cc_addresses=tuple(address for address, _ in cc),
        names={address: name for address, name in (*to, *cc) if name},
        thread_id=payload.get("conversationId", "") or "",
        sent_at=_stamp(payload.get("sentDateTime", "") or ""),
        is_draft=bool(payload.get("isDraft")),
    )


def to_event(payload: dict) -> MailboxEvent:
    """One Graph message payload as a provider-neutral MailboxEvent.

    This is the only place Graph's field names are allowed to appear on the
    read path; everything downstream works on the DTO.
    """
    body = payload.get("body") or {}
    content = body.get("content") or payload.get("bodyPreview") or ""
    recipients: list[str] = []
    for key in ("toRecipients", "ccRecipients"):
        for entry in payload.get(key) or []:
            address = ((entry or {}).get("emailAddress") or {}).get("address", "")
            if address:
                recipients.append(address.lower())
    sender = ((payload.get("from") or {}).get("emailAddress") or {}).get("address", "") or ""
    return MailboxEvent(
        native_id=payload.get("id", "") or "",
        from_address=sender.lower(),
        subject=payload.get("subject", "") or "",
        body=content,
        body_is_html=(body.get("contentType") or "").lower() == "html",
        to_addresses=tuple(recipients),
        thread_id=payload.get("conversationId", "") or "",
        rfc_message_id=payload.get("internetMessageId", "") or "",
        # Graph does not return In-Reply-To in the delta projection, and asking
        # for internetMessageHeaders per message would double the call count
        # for a match conversationId already makes.
        in_reply_to="",
        received_at=_stamp(payload.get("receivedDateTime", "") or ""),
        sent_at=_stamp(payload.get("sentDateTime", "") or ""),
        is_draft=bool(payload.get("isDraft")),
        removed=payload.get("@removed") is not None,
        web_link=payload.get("webLink", "") or "",
        has_attachments=bool(payload.get("hasAttachments")),
    )


# Which GraphClient methods serve which logical folder. Keeping the original
# per-folder method names means existing fake clients still satisfy the seam.
_FOLDER_METHODS = {
    FOLDER_INBOX: ("inbox_delta_baseline", "inbox_delta"),
    FOLDER_SENT: ("sent_delta_baseline", "sent_delta"),
}


class MicrosoftProvider:
    """MailProvider over Microsoft Graph.

    A thin adapter: the OAuth and HTTP work above is unchanged, this only gives
    it the shape sync.py and the router now talk to. ``client_factory`` exists
    so tests can inject a fake GraphClient without faking the whole provider.
    """

    name = "microsoft"

    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory or GraphClient

    def _client(self, db, connection: MailboxConnection):
        return self._client_factory(db, connection)

    def is_configured(self) -> bool:
        return is_configured()

    def redirect_uri(self) -> str:
        return redirect_uri()

    def authorize_url(self, state: str) -> str:
        return authorize_url(state)

    def exchange_code(self, code: str, params) -> ConnectResult:
        tokens = exchange_code(code)
        profile = fetch_profile(tokens["access_token"])
        return ConnectResult(
            tokens=tokens,
            address=profile["address"],
            display_name=profile["display_name"],
            provider_account_id=profile["provider_account_id"],
        )

    def build_sender(self, db, connection: MailboxConnection):
        return GraphSender(db, connection, client=self._client(db, connection))

    def read_cursor(self, connection: MailboxConnection, folder: str) -> str:
        if folder == FOLDER_INBOX:
            return connection.inbox_delta_link
        if folder == FOLDER_SENT:
            return connection.sent_delta_link
        return ""

    def iter_folder(self, db, connection: MailboxConnection, folder: str, *, cursor: str):
        baseline_name, walk_name = _FOLDER_METHODS[folder]
        client = self._client(db, connection)
        if not cursor:
            # First pass: record where "now" is and read nothing. Old mail is
            # not outreach traffic.
            return FolderPage(next_cursor=getattr(client, baseline_name)(), initialized=True)
        payloads, delta_link = getattr(client, walk_name)(cursor)
        payloads.sort(key=lambda item: item.get("receivedDateTime") or "")
        return FolderPage(
            events=[to_event(payload) for payload in payloads],
            next_cursor=delta_link or cursor,
        )

    def commit_cursor(self, db, connection: MailboxConnection, folder: str, cursor: str) -> None:
        if not cursor:
            return
        if folder == FOLDER_INBOX:
            connection.inbox_delta_link = cursor
        elif folder == FOLDER_SENT:
            connection.sent_delta_link = cursor

    def place_reply_draft(self, db, *, connection, inbound, html_body: str) -> DraftResult:
        draft = self._client(db, connection).create_reply_draft(
            inbound.provider_native_id, html_body
        )
        return DraftResult(
            provider_draft_id=draft.get("id", ""), web_link=draft.get("webLink", "")
        )

    def iter_sent_history(
        self, db, connection: MailboxConnection, *, since, cursor: str = ""
    ) -> SweepPage:
        payloads, next_link = self._client(db, connection).sent_history(since, cursor=cursor)
        records = [to_sweep_record(payload) for payload in payloads]
        # Graph orders newest-first and $filter already bounds the window, but
        # a stale nextLink can still hand back older mail; drop it here so the
        # caller's window is the one that counts.
        records = [r for r in records if r.sent_at is None or r.sent_at >= since]
        return SweepPage(
            records=records, next_cursor=next_link, has_more=bool(next_link)
        )

    def search_participant(self, db, connection: MailboxConnection, address: str):
        found = self._client(db, connection).search_participant(address)
        return [to_event(payload) for payload in found]

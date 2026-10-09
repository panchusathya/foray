"""Gmail / Google Workspace: OAuth for recruiter mailboxes, sending, and sync.

Same posture as microsoft.py and zoho.py -- hand-rolled auth-code flow over
httpx, tokens stored encrypted by us, no Google SDK -- and the same contract:
providers own OAuth, sending and reading raw mailbox traffic, and nothing
downstream knows which provider a ``MailboxEvent`` came from.

What differs from the other two, each of which fails silently rather than
loudly if it is got wrong:

* **Granular consent.** Google lets the person untick individual scopes on
  the consent screen. A mailbox connected without ``gmail.send`` would look
  fine and fail every send, so the callback refuses a partial grant.

* **Threading is the thread id plus headers.** Gmail files a message into a
  thread only when the send names its ``threadId`` *and* carries matching
  In-Reply-To/References. The sequencer hands us the parent's *message* id
  (``thread_native_id``); Gmail wants the thread's, so it is looked up. A
  failed lookup sends unthreaded rather than not at all -- the same bargain
  ``OutboundEmail`` documents for every provider.

* **Message-ID is read back, not assumed.** Gmail generally keeps the
  Message-ID we set but does not promise to, and reply matching keys on it, so
  after a send the header Gmail actually stored is fetched.

* **History, not polling.** Gmail exposes a mailbox-wide ``historyId``; the
  cursor is that id plus a timestamp. History expires (Google documents "about
  a week", sometimes less), and an expired id answers 404 -- at which point the
  folder is caught up by date instead of being re-baselined, which would skip
  everything in the gap.

* **Times come from ``internalDate``.** It is Gmail's own epoch for when the
  message entered the mailbox. The Date header is whatever the sending client
  wrote, which is the class of bug that froze sending on Zoho for a day.

Scopes: ``gmail.readonly`` (restricted) to sync replies, ``gmail.send`` to send
and ``gmail.compose`` to place reply drafts. Restricted scopes on an External
OAuth app need Google verification and a security assessment before leaving
Testing mode; an Internal app on the sending Workspace needs neither. That is
an ops decision recorded in config.py, not a code path.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, make_msgid, parseaddr
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

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
REQUIRED_SCOPES = (SCOPE_READ, SCOPE_SEND, SCOPE_COMPOSE)
SCOPES = " ".join(("openid", "email", "profile", *REQUIRED_SCOPES))

LABELS = {FOLDER_INBOX: "INBOX", FOLDER_SENT: "SENT"}

_TOKEN_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 30.0
_PAGE_SIZE = 100
# How many history (or catch-up listing) pages one pass reads before it stops
# and leaves the cursor where it was. Re-reading is free -- ingest is idempotent
# on the native id -- skipping is not.
_MAX_PAGES = 20
_SWEEP_PAGE_SIZE = 200
_RETRYABLE = {429, 500, 502, 503}


class GmailError(ProviderError):
    """Gmail-side failure; a ProviderError so one broken mailbox does not stop
    the sync loop for the rest."""


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.gmail_client_id and settings.gmail_client_secret)


def redirect_uri() -> str:
    settings = get_settings()
    if settings.gmail_redirect_uri:
        return settings.gmail_redirect_uri
    return f"{settings.base_url.rstrip('/')}/v1/integrations/gmail/callback"


def authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": get_settings().gmail_client_id,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            # offline yields a refresh token; consent forces it to be issued
            # again on a reconnect, and select_account stops a second mailbox
            # being silently connected as whichever one the browser had open.
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
            "state": state,
        }
    )
    return f"{AUTH_URL}?{query}"


def _token_request(grant: dict) -> dict:
    settings = get_settings()
    data = {
        "client_id": settings.gmail_client_id,
        "client_secret": settings.gmail_client_secret,
        **grant,
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.post(TOKEN_URL, data=data)
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or payload.get("error"):
        detail = payload.get("error_description") or payload.get("error") or response.text[:300]
        raise GmailError(f"token request failed: {detail}", status_code=response.status_code)
    if not payload.get("access_token"):
        raise GmailError("token request returned no access token")
    return {
        "access_token": payload["access_token"],
        # Google returns a refresh token only on the consent exchange; a refresh
        # omits it and the caller keeps the old one.
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", ""),
    }


def exchange_code(code: str) -> dict:
    return _token_request(
        {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri()}
    )


def refresh_tokens(refresh_token: str) -> dict:
    return _token_request({"grant_type": "refresh_token", "refresh_token": refresh_token})


def missing_scopes(granted: str) -> list[str]:
    have = set((granted or "").split())
    return [scope for scope in REQUIRED_SCOPES if scope not in have]


# ---- wire-format helpers ------------------------------------------------------


def _epoch_ms(raw) -> datetime | None:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC) if value > 0 else None


def _headers(payload: dict) -> dict[str, str]:
    """Header name (lower-cased) -> value. First occurrence wins."""
    out: dict[str, str] = {}
    for header in (payload or {}).get("headers") or []:
        name = str(header.get("name") or "").lower()
        if name and name not in out:
            out[name] = str(header.get("value") or "")
    return out


def _one(raw: str) -> str:
    return (parseaddr(raw or "")[1] or "").strip().lower()


def _named(values: list[str]) -> list[tuple[str, str]]:
    """(address, display name) from To/Cc header values. getaddresses copes with
    "Smith, John" <js@x.com>, which a comma split turns into two people."""
    out = []
    for name, address in getaddresses([v for v in values if v]):
        address = (address or "").strip().lower()
        if "@" in address:
            out.append((address, (name or "").strip()))
    return out


def _decode(data: str) -> bytes:
    if not data:
        return b""
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict) -> str:
    content_type = _headers(part).get("content-type", "")
    match = re.search(r'charset="?([\w.-]+)"?', content_type, re.I)
    return match.group(1) if match else "utf-8"


def _walk(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _walk(child)


def _body(payload: dict) -> tuple[str, bool, bool]:
    """(body, is_html, has_attachments) from a format=full payload.

    HTML is preferred over plain text, matching what the other providers
    return; a nested multipart/related or /mixed is walked, not just the top.
    """
    html_text = plain_text = ""
    attachments = False
    for part in _walk(payload or {}):
        if part.get("filename") and (part.get("body") or {}).get("attachmentId"):
            attachments = True
            continue
        mime = str(part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        text = _decode(data).decode(_charset(part), errors="replace")
        if mime == "text/html" and not html_text:
            html_text = text
        elif mime == "text/plain" and not plain_text:
            plain_text = text
    if html_text:
        return html_text, True, attachments
    return plain_text, False, attachments


def web_link(address: str, message_id: str) -> str:
    return (
        f"https://mail.google.com/mail/?authuser={address}#all/{message_id}" if message_id else ""
    )


def to_event(message: dict, *, address: str = "") -> MailboxEvent:
    """One Gmail message (format=full) as a provider-neutral MailboxEvent."""
    payload = message.get("payload") or {}
    headers = _headers(payload)
    labels = set(message.get("labelIds") or [])
    body, is_html, attachments = _body(payload)
    when = _epoch_ms(message.get("internalDate"))
    return MailboxEvent(
        native_id=str(message.get("id") or ""),
        from_address=_one(headers.get("from", "")),
        subject=headers.get("subject", ""),
        body=body or message.get("snippet", "") or "",
        body_is_html=is_html,
        to_addresses=tuple(a for a, _ in _named([headers.get("to", ""), headers.get("cc", "")])),
        thread_id=str(message.get("threadId") or ""),
        rfc_message_id=headers.get("message-id", "").strip(),
        in_reply_to=" ".join(headers.get("in-reply-to", "").split()),
        reply_to=_one(headers.get("reply-to", "")),
        # internalDate for both: see the module docstring.
        received_at=when,
        sent_at=when,
        is_draft="DRAFT" in labels,
        removed="TRASH" in labels,
        web_link=web_link(address, str(message.get("id") or "")),
        has_attachments=attachments,
    )


def build_mime(
    *,
    from_address: str,
    from_name: str,
    to: str,
    subject: str,
    text_body: str,
    html_body: str,
    message_id: str,
    in_reply_to: str = "",
    references: str = "",
    extra_headers: dict | None = None,
) -> EmailMessage:
    """An RFC 5322 message. EmailMessage refuses a header value containing a
    line break, which is what keeps a subject or a display name from smuggling
    in a Bcc."""
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_address)) if from_name else from_address
    message["To"] = to
    message["Subject"] = subject
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    for name, value in (extra_headers or {}).items():
        message[name] = value
    message.set_content(text_body or "")
    if html_body:
        message.add_alternative(html_body, subtype="html")
    return message


def _raw(message: EmailMessage) -> str:
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


# ---- HTTP client --------------------------------------------------------------


class GmailClient:
    """Authenticated Gmail API calls for one connected mailbox.

    Refreshes within a minute of expiry and persists the new token blob; a
    failed refresh marks the connection ``error`` so Settings shows it, instead
    of every send failing the same way with nobody looking.
    """

    def __init__(self, db, connection: MailboxConnection) -> None:
        self.db = db
        self.connection = connection

    def _access_token(self) -> str:
        tokens = decrypt_json(self.connection.token_blob)
        if tokens["expires_at"] - _TOKEN_SKEW_SECONDS > time.time():
            return tokens["access_token"]
        try:
            fresh = refresh_tokens(tokens.get("refresh_token", ""))
        except GmailError as exc:
            self.connection.status = "error"
            self.connection.error = (
                f"{exc} -- reconnect this mailbox (Google revoked or expired the grant)"
            )
            self.db.flush()
            raise
        if not fresh["refresh_token"]:
            fresh["refresh_token"] = tokens.get("refresh_token", "")
        self.connection.token_blob = encrypt_json(fresh)
        self.connection.status = "active"
        self.connection.error = ""
        self.db.flush()
        return fresh["access_token"]

    def _request(self, method: str, path: str, *, params=None, json_body=None) -> dict:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        url = f"{API_BASE}{path}"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.request(method, url, params=params, json=json_body, headers=headers)
            limited = response.status_code == 403 and "ratelimitexceeded" in response.text.lower()
            if response.status_code in _RETRYABLE or limited:
                # Honour one bounded back-off; the job queue owns real retries.
                delay = min(float(response.headers.get("Retry-After") or 2), 30.0)
                time.sleep(delay)
                response = http.request(method, url, params=params, json=json_body, headers=headers)
        if response.status_code >= 400:
            raise GmailError(
                f"gmail {method} {path} failed ({response.status_code}): {response.text[:400]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except ValueError:
            return {}

    def get_profile(self) -> dict:
        return self._request("GET", "/profile")

    def list_history(self, start_history_id: str, label_id: str, page_token: str = "") -> dict:
        params = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "labelId": label_id,
            "maxResults": _PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", "/history", params=params)

    def list_messages(
        self, *, q: str = "", label_ids=(), page_token: str = "", max_results: int = _PAGE_SIZE
    ) -> dict:
        params: list[tuple[str, str]] = [("maxResults", str(max_results))]
        if q:
            params.append(("q", q))
        for label in label_ids:
            params.append(("labelIds", label))
        if page_token:
            params.append(("pageToken", page_token))
        return self._request("GET", "/messages", params=params)

    def get_message(self, message_id: str, *, fmt: str = "full", headers=()) -> dict:
        params: list[tuple[str, str]] = [("format", fmt)]
        for name in headers:
            params.append(("metadataHeaders", name))
        return self._request("GET", f"/messages/{message_id}", params=params)

    def send(self, raw: str, thread_id: str = "") -> dict:
        body = {"raw": raw}
        if thread_id:
            body["threadId"] = thread_id
        return self._request("POST", "/messages/send", json_body=body)

    def create_draft(self, raw: str, thread_id: str = "") -> dict:
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return self._request("POST", "/drafts", json_body={"message": message})


# ---- sending ------------------------------------------------------------------


class GmailSender:
    """EmailSender that sends as one connected Gmail mailbox.

    From is always the mailbox itself. Gmail would accept a configured send-as
    alias, but connecting a mailbox means outreach goes out as that identity --
    the same rule ZohoSender and GraphSender follow.
    """

    name = "gmail"

    def __init__(self, db, connection: MailboxConnection, client: GmailClient | None = None):
        self.connection = connection
        self.client = client or GmailClient(db, connection)

    def _thread_of(self, native_id: str) -> str:
        if not native_id:
            return ""
        try:
            return str(self.client.get_message(native_id, fmt="minimal").get("threadId") or "")
        except GmailError as exc:
            logger.warning(
                "gmail thread lookup for %s failed, sending unthreaded: %s", native_id, exc
            )
            return ""

    def send(self, email):
        from ..outreach.sender import SendResult

        address = self.connection.address
        domain = address.rsplit("@", 1)[-1] if "@" in address else None
        generated_id = make_msgid(domain=domain)
        wanted = (getattr(email, "from_email", "") or "").strip().lower()
        if wanted and wanted != address.lower():
            logger.info("gmail send: ignoring From %s, sending as %s", wanted, address)
        try:
            mime = build_mime(
                from_address=address,
                from_name=self.connection.display_name or getattr(email, "from_name", "") or "",
                to=email.to,
                subject=email.subject,
                text_body=email.text_body,
                html_body=email.html_body,
                message_id=generated_id,
                in_reply_to=email.in_reply_to,
                references=email.references,
                extra_headers=getattr(email, "headers", None) or {},
            )
        except ValueError as exc:
            # A line break in a header value: refuse rather than send something
            # other than what was written.
            return SendResult(ok=False, error=f"refused to build message: {exc}")
        thread_id = self._thread_of(getattr(email, "thread_native_id", ""))
        try:
            data = self.client.send(_raw(mime), thread_id=thread_id)
        except GmailError as exc:
            logger.error("gmail send failed for %s: %s", address, exc)
            return SendResult(ok=False, error=str(exc))
        native_id = str(data.get("id") or "")
        if not native_id:
            logger.error("gmail send for %s returned no id: %s", address, str(data)[:200])
            return SendResult(ok=False, error="gmail send returned no message id")
        rfc_id = generated_id
        try:
            stored = self.client.get_message(native_id, fmt="metadata", headers=("Message-ID",))
            rfc_id = _headers(stored.get("payload") or {}).get("message-id", "").strip() or rfc_id
        except GmailError as exc:
            # The send went out; only the read-back failed. Keep the id we set,
            # which Gmail keeps in practice.
            logger.warning("gmail Message-ID read-back failed for %s: %s", native_id, exc)
        return SendResult(
            ok=True,
            provider_message_id=rfc_id,
            provider_native_id=native_id,
            internet_message_id=rfc_id,
            conversation_id=str(data.get("threadId") or "") or thread_id,
        )


# ---- cursors ------------------------------------------------------------------


def _cursor_state(cursor: str) -> tuple[str, int]:
    """(history id, high-water internalDate in ms)."""
    if not cursor:
        return "", 0
    try:
        data = json.loads(cursor)
    except ValueError:
        return "", 0
    return str(data.get("history") or ""), int(data.get("ts") or 0)


def _encode_cursor(history_id: str, ts_ms: int) -> str:
    return json.dumps({"history": str(history_id), "ts": int(ts_ms)})


# ---- provider -----------------------------------------------------------------


class GmailProvider:
    """MailProvider over the Gmail REST API."""

    name = "gmail"

    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory or GmailClient

    def _client(self, db, connection: MailboxConnection) -> GmailClient:
        return self._client_factory(db, connection)

    # ---- OAuth -----------------------------------------------------------

    def is_configured(self) -> bool:
        return is_configured()

    def redirect_uri(self) -> str:
        return redirect_uri()

    def authorize_url(self, state: str) -> str:
        return authorize_url(state)

    def exchange_code(self, code: str, params) -> ConnectResult:
        tokens = exchange_code(code)
        missing = missing_scopes(tokens.get("scope", ""))
        if missing:
            # Granular consent let the person untick a permission. Connecting
            # anyway would fail every send (or every sync) later, far from here.
            names = ", ".join(scope.rsplit("/", 1)[-1] for scope in missing)
            raise GmailError(
                f"Google did not grant {names}. Reconnect and leave every Gmail permission ticked."
            )
        if not tokens.get("refresh_token"):
            raise GmailError("Google returned no refresh token; reconnect to grant offline access")
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            profile = http.get(f"{API_BASE}/profile", headers=headers)
            info = http.get(USERINFO_URL, headers=headers)
        if profile.status_code >= 400:
            raise GmailError(
                f"gmail profile failed ({profile.status_code}): {profile.text[:300]}",
                status_code=profile.status_code,
            )
        address = str(profile.json().get("emailAddress") or "").strip().lower()
        if not address:
            raise GmailError("Gmail profile has no email address")
        identity = info.json() if info.status_code < 400 else {}
        return ConnectResult(
            tokens=tokens,
            address=address,
            display_name=str(identity.get("name") or ""),
            provider_account_id=str(identity.get("sub") or ""),
            sync_state={"cursors": {}},
        )

    # ---- sending ---------------------------------------------------------

    def build_sender(self, db, connection: MailboxConnection):
        return GmailSender(db, connection, client=self._client(db, connection))

    # ---- reading ---------------------------------------------------------

    def read_cursor(self, connection: MailboxConnection, folder: str) -> str:
        return ((connection.sync_state or {}).get("cursors") or {}).get(folder, "")

    def commit_cursor(self, db, connection: MailboxConnection, folder: str, cursor: str) -> None:
        if not cursor:
            return
        # Reassign, never mutate: SQLAlchemy does not see an in-place JSON edit,
        # and an unsaved cursor re-reads the same mail on every pass forever.
        state = dict(connection.sync_state or {})
        cursors = dict(state.get("cursors") or {})
        cursors[folder] = cursor
        state["cursors"] = cursors
        connection.sync_state = state

    def iter_folder(
        self, db, connection: MailboxConnection, folder: str, *, cursor: str
    ) -> FolderPage:
        label = LABELS.get(folder)
        if label is None:
            raise GmailError(f"unknown folder {folder!r}")
        client = self._client(db, connection)
        history_id, ts_ms = _cursor_state(cursor)

        if not history_id:
            # Baseline: remember where "now" is and read nothing. Old mail is
            # not outreach traffic.
            profile = client.get_profile()
            return FolderPage(
                next_cursor=_encode_cursor(
                    str(profile.get("historyId") or ""), int(time.time() * 1000)
                ),
                initialized=True,
            )

        try:
            ids, latest_history, complete = self._history_ids(client, history_id, label)
        except GmailError as exc:
            if exc.status_code != 404:
                raise
            # The history id has expired. Catch up by date rather than
            # re-baselining, which would skip everything in the gap.
            logger.warning(
                "gmail history expired for %s/%s; catching up by date",
                connection.address,
                folder,
            )
            ids, complete = self._ids_since(client, ts_ms, label)
            latest_history = str(client.get_profile().get("historyId") or history_id)

        events = []
        newest_ts = ts_ms
        for message_id in ids:
            try:
                message = client.get_message(message_id, fmt="full")
            except GmailError as exc:
                if exc.status_code == 404:
                    continue  # deleted between listing and fetching
                raise
            if label not in set(message.get("labelIds") or []):
                continue
            event = to_event(message, address=connection.address)
            events.append(event)
            stamp = int(str(message.get("internalDate") or "0") or 0)
            newest_ts = max(newest_ts, stamp)

        if not complete:
            # Ran out of pages before the end: keep the old cursor so the next
            # pass reads the rest. Re-reading is free; a gap is permanent.
            logger.warning(
                "gmail catch-up for %s/%s exceeded %d pages; cursor held back",
                connection.address,
                folder,
                _MAX_PAGES,
            )
            return FolderPage(events=events, next_cursor="")
        return FolderPage(events=events, next_cursor=_encode_cursor(latest_history, newest_ts))

    def _history_ids(self, client: GmailClient, start: str, label: str):
        ids: list[str] = []
        seen: set[str] = set()
        latest = start
        page_token = ""
        for _ in range(_MAX_PAGES):
            page = client.list_history(start, label, page_token)
            latest = str(page.get("historyId") or latest)
            for record in page.get("history") or []:
                for added in record.get("messagesAdded") or []:
                    message = added.get("message") or {}
                    message_id = str(message.get("id") or "")
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        ids.append(message_id)
            page_token = page.get("nextPageToken") or ""
            if not page_token:
                return ids, latest, True
        return ids, start, False

    def _ids_since(self, client: GmailClient, ts_ms: int, label: str):
        after = max(int(ts_ms / 1000) - 60, 0)  # a minute's overlap; dedupe is free
        ids: list[str] = []
        page_token = ""
        for _ in range(_MAX_PAGES):
            page = client.list_messages(
                q=f"after:{after}", label_ids=(label,), page_token=page_token
            )
            ids.extend(str(m.get("id")) for m in page.get("messages") or [] if m.get("id"))
            page_token = page.get("nextPageToken") or ""
            if not page_token:
                return list(reversed(ids)), True  # listing is newest-first
        return list(reversed(ids)), False

    def iter_sent_history(
        self, db, connection: MailboxConnection, *, since, cursor: str = ""
    ) -> SweepPage:
        """One page of Sent mail back to ``since``, for contact discovery.

        Metadata only: each message is fetched with ``format=metadata`` and the
        From/To/Cc headers, so no body or subject is ever requested -- the
        guarantee ``SweepRecord`` exists to make structural. The cursor is the
        listing's own page token and lives in ``discovery_state``.
        """
        client = self._client(db, connection)
        page = client.list_messages(
            q=f"after:{int(since.timestamp())}",
            label_ids=("SENT",),
            page_token=cursor or "",
            max_results=_SWEEP_PAGE_SIZE,
        )
        records: list[SweepRecord] = []
        for item in page.get("messages") or []:
            message_id = str(item.get("id") or "")
            if not message_id:
                continue
            try:
                message = client.get_message(
                    message_id, fmt="metadata", headers=("From", "To", "Cc")
                )
            except GmailError as exc:
                if exc.status_code == 404:
                    continue
                raise
            headers = _headers(message.get("payload") or {})
            to = _named([headers.get("to", "")])
            cc = _named([headers.get("cc", "")])
            records.append(
                SweepRecord(
                    native_id=message_id,
                    from_address=_one(headers.get("from", "")),
                    to_addresses=tuple(a for a, _ in to),
                    cc_addresses=tuple(a for a, _ in cc),
                    names={a: n for a, n in (*to, *cc) if n},
                    thread_id=str(message.get("threadId") or ""),
                    sent_at=_epoch_ms(message.get("internalDate")),
                    is_draft="DRAFT" in set(message.get("labelIds") or []),
                )
            )
        next_token = page.get("nextPageToken") or ""
        return SweepPage(records=records, next_cursor=next_token, has_more=bool(next_token))

    # ---- drafts and search ----------------------------------------------

    def place_reply_draft(self, db, *, connection, inbound, html_body: str) -> DraftResult:
        """The generated reply, in the mailbox's own Drafts, on the thread."""
        from .zoho import _quote_original, _reply_subject

        client = self._client(db, connection)
        address = connection.address
        domain = address.rsplit("@", 1)[-1] if "@" in address else None
        parent = getattr(inbound, "internet_message_id", "") or ""
        mime = build_mime(
            from_address=address,
            from_name=connection.display_name or "",
            to=getattr(inbound, "from_address", "") or getattr(inbound, "to_address", ""),
            subject=_reply_subject(getattr(inbound, "subject", "")),
            text_body="",
            html_body=html_body + _quote_original(inbound),
            message_id=make_msgid(domain=domain),
            in_reply_to=parent,
            references=parent,
        )
        thread_id = getattr(inbound, "conversation_id", "") or ""
        data = client.create_draft(_raw(mime), thread_id=thread_id)
        draft_message = (data.get("message") or {}).get("id") or ""
        return DraftResult(
            provider_draft_id=str(data.get("id") or ""),
            web_link=web_link(address, draft_message),
        )

    def search_participant(self, db, connection: MailboxConnection, address: str):
        client = self._client(db, connection)
        quoted = address.replace('"', "").strip()
        page = client.list_messages(q=f"from:{quoted} OR to:{quoted}", max_results=25)
        events = []
        for item in page.get("messages") or []:
            try:
                message = client.get_message(str(item.get("id")), fmt="full")
            except GmailError as exc:
                if exc.status_code == 404:
                    continue
                raise
            events.append(to_event(message, address=connection.address))
        return events

"""Zoho Mail: OAuth for the recruiter's Zoho mailboxes, sending, and polling.

Shaped like microsoft.py on purpose -- hand-rolled auth-code flow over httpx,
tokens stored encrypted by us -- but three things differ enough to be worth
stating up front, because each one silently produces wrong behaviour rather
than an error:

* **Data centres.** The authorize call goes to whichever accounts host we were
  configured with, but the callback comes back carrying ``accounts-server``,
  and *that* is the host the token exchange and every later refresh must use.
  The Mail API base is the same host with ``accounts.`` rewritten to ``mail.``.
  The token response also carries ``api_domain`` (``zohoapis.*``) -- that is
  not the Mail host and using it yields 404s on every call.

* **Auth header.** ``Authorization: Zoho-oauthtoken <token>``, not ``Bearer``.

* **No delta, no webhooks.** Folders are polled newest-first and compared
  against a per-folder high-water mark kept in ``sync_state``. The list view
  returns a summary rather than a body, so each genuinely new message costs one
  extra content fetch -- which is why the inbound match tries thread id before
  falling back to headers.

Zoho accepts no custom message headers, so the ``X-Juicebox-Message-Id`` header
the Graph path attaches is simply absent here. Nothing depends on it: the send
response returns both the native ``messageId`` and the RFC ``mailId``, which is
what reply matching and ingest idempotency actually key on.
"""

from __future__ import annotations

import json
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
)

logger = logging.getLogger(__name__)

# Send, read messages and folders, and identify the account. accounts.READ is
# what turns an access token into "which mailbox is this and what is its id".
SCOPES = ",".join(
    [
        "ZohoMail.accounts.READ",
        "ZohoMail.messages.ALL",
        "ZohoMail.folders.READ",
    ]
)

_TOKEN_SKEW_SECONDS = 60
_HTTP_TIMEOUT = 30.0
# Zoho caps a page at 200 and `start` is 1-based.
_PAGE_LIMIT = 200
# How far one pass will page back trying to reach the high-water mark. Deep
# enough for any realistic worker outage (25 * 200 = 5000 messages); if it is
# ever exhausted the mark deliberately does not advance, so nothing is skipped
# -- the next pass carries on and the warning says so.
_MAX_CATCHUP_PAGES = 25

# Zoho's own folder names, mapped from the logical ones sync.py asks for.
_FOLDER_NAMES = {FOLDER_INBOX: "inbox", FOLDER_SENT: "sent"}
_DRAFTS = "drafts"


class ZohoError(ProviderError):
    """Zoho-side failure. A ProviderError so the sync loop can survive one
    broken mailbox without knowing which provider it belongs to."""


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.zoho_client_id and settings.zoho_client_secret)


def redirect_uri() -> str:
    settings = get_settings()
    if settings.zoho_redirect_uri:
        return settings.zoho_redirect_uri
    return f"{settings.base_url.rstrip('/')}/v1/integrations/zoho/callback"


def accounts_base() -> str:
    return get_settings().zoho_accounts_domain.rstrip("/")


def authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": get_settings().zoho_client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri(),
            "scope": SCOPES,
            # offline yields a refresh token; consent forces the prompt so a
            # second mailbox on the same browser session is not silently
            # authorised as the first one.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
    )
    return f"{accounts_base()}/oauth/v2/auth?{query}"


def api_base_for(auth_base: str) -> str:
    """The Mail API host that pairs with an accounts host.

    accounts.zoho.eu -> mail.zoho.eu. Deliberately not the token response's
    api_domain, which is zohoapis.* and serves a different set of APIs.
    """
    host = auth_base.rstrip("/")
    if "://" in host:
        scheme, _, rest = host.partition("://")
    else:
        scheme, rest = "https", host
    if rest.startswith("accounts."):
        rest = "mail." + rest[len("accounts.") :]
    return f"{scheme}://{rest}"


def resolve_auth_base(params) -> str:
    """Which accounts host this callback belongs to.

    Zoho returns ``accounts-server`` (and a coarser ``location``) on the
    redirect. Honouring it is the difference between a working connection and
    an opaque ``invalid_code``.
    """
    server = (params or {}).get("accounts-server") or ""
    if server:
        server = server.rstrip("/")
        return server if "://" in server else f"https://{server}"
    return accounts_base()


def _token_request(auth_base: str, grant: dict) -> dict:
    settings = get_settings()
    data = {
        "client_id": settings.zoho_client_id,
        "client_secret": settings.zoho_client_secret,
        **grant,
    }
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.post(f"{auth_base.rstrip('/')}/oauth/v2/token", data=data)
    if response.status_code >= 400:
        raise ZohoError(
            f"token request failed: {response.text[:300]}", status_code=response.status_code
        )
    payload = response.json()
    # Zoho answers 200 with an {"error": ...} body for bad codes and bad
    # redirect URIs, so the status code alone is not the success signal.
    if payload.get("error"):
        raise ZohoError(f"token request failed: {payload['error']}")
    if not payload.get("access_token"):
        raise ZohoError(f"token request returned no access token: {response.text[:300]}")
    return {
        "access_token": payload["access_token"],
        # Only the first consent returns one; a refresh keeps the old.
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
    }


def exchange_code(code: str, auth_base: str) -> dict:
    return _token_request(
        auth_base,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
        },
    )


def refresh_tokens(refresh_token: str, auth_base: str) -> dict:
    return _token_request(
        auth_base, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def _epoch_ms(raw) -> datetime | None:
    """Zoho timestamps are millisecond epochs, and arrive as strings."""
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _addresses(raw) -> list[str]:
    """Zoho joins recipients into one comma-separated string."""
    if not raw:
        return []
    parts = raw if isinstance(raw, list) else str(raw).split(",")
    out = []
    for part in parts:
        address = (part or "").strip()
        # Pull the address out of "Display Name <a@b>" before stripping the
        # brackets, or the display name survives as part of the address.
        if "<" in address and ">" in address:
            address = address[address.index("<") + 1 : address.index(">")]
        address = address.strip().strip("<>").lower()
        if address:
            out.append(address)
    return out


def _one_address(raw) -> str:
    found = _addresses(raw)
    return found[0] if found else ""


class ZohoClient:
    """Authenticated Zoho Mail calls for one connected mailbox.

    Mirrors GraphClient: refreshes inside a minute of expiry, persists the new
    blob, and marks the connection ``error`` on a failed refresh so the UI
    surfaces it instead of every send failing identically.
    """

    def __init__(self, db, connection: MailboxConnection) -> None:
        self.db = db
        self.connection = connection

    @property
    def api_base(self) -> str:
        base = self.connection.api_base or api_base_for(
            self.connection.auth_base or accounts_base()
        )
        return base.rstrip("/")

    @property
    def account_id(self) -> str:
        if not self.connection.provider_account_id:
            raise ZohoError(f"mailbox {self.connection.address} has no Zoho account id")
        return self.connection.provider_account_id

    def _access_token(self) -> str:
        tokens = decrypt_json(self.connection.token_blob)
        if tokens["expires_at"] - _TOKEN_SKEW_SECONDS > time.time():
            return tokens["access_token"]
        auth_base = self.connection.auth_base or accounts_base()
        try:
            fresh = refresh_tokens(tokens["refresh_token"], auth_base)
        except ZohoError as exc:
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

    def _request(self, method: str, path: str, *, json_body: dict | None = None):
        headers = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"}
        url = f"{self.api_base}{path}"
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.request(method, url, json=json_body, headers=headers)
            if response.status_code in (429, 503):
                # Zoho documents per-API rate limits without publishing the
                # numbers. Honour Retry-After once, bounded; the job queue owns
                # real backoff.
                delay = min(float(response.headers.get("Retry-After") or 2), 30.0)
                time.sleep(delay)
                response = http.request(method, url, json=json_body, headers=headers)
        if response.status_code >= 400:
            raise ZohoError(
                f"zoho {method} {path} failed ({response.status_code}): {response.text[:500]}",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload.get("data", payload)

    # ---- account and folder discovery ------------------------------------

    def fetch_accounts(self) -> list[dict]:
        data = self._request("GET", "/api/accounts")
        return data if isinstance(data, list) else [data]

    def fetch_folders(self) -> list[dict]:
        data = self._request("GET", f"/api/accounts/{self.account_id}/folders")
        return data if isinstance(data, list) else [data]

    # ---- messages --------------------------------------------------------

    def list_folder(self, folder_id: str, *, start: int, limit: int) -> list[dict]:
        path = (
            f"/api/accounts/{self.account_id}/messages/view"
            f"?folderId={folder_id}&start={start}&limit={limit}"
        )
        data = self._request("GET", path)
        return data if isinstance(data, list) else []

    def fetch_content(self, folder_id: str, message_id: str) -> dict:
        path = (
            f"/api/accounts/{self.account_id}/folders/{folder_id}"
            f"/messages/{message_id}/content"
        )
        data = self._request("GET", path)
        return data if isinstance(data, dict) else {}

    def fetch_header(self, folder_id: str, message_id: str) -> dict:
        path = (
            f"/api/accounts/{self.account_id}/folders/{folder_id}"
            f"/messages/{message_id}/header"
        )
        data = self._request("GET", path)
        return data if isinstance(data, dict) else {}

    def send_message(self, body: dict) -> dict:
        data = self._request(
            "POST", f"/api/accounts/{self.account_id}/messages", json_body=body
        )
        return data if isinstance(data, dict) else {}

    def search(self, search_key: str, *, limit: int = 25) -> list[dict]:
        path = (
            f"/api/accounts/{self.account_id}/messages/search"
            f"?searchKey={search_key}&start=1&limit={min(limit, _PAGE_LIMIT)}"
        )
        data = self._request("GET", path)
        return data if isinstance(data, list) else []


def to_event(payload: dict, *, body: str = "", in_reply_to: str = "") -> MailboxEvent:
    """One Zoho message payload as a provider-neutral MailboxEvent.

    The list view has no body, so ``body`` is supplied separately by whoever
    decided the message was worth a content fetch.
    """
    received = _epoch_ms(payload.get("receivedTime"))
    sent = _epoch_ms(payload.get("sentDateInGMT") or payload.get("sentTime"))
    return MailboxEvent(
        native_id=str(payload.get("messageId") or ""),
        from_address=_one_address(payload.get("fromAddress")),
        subject=payload.get("subject", "") or "",
        body=body or payload.get("summary", "") or "",
        # /content returns HTML; the summary fallback is plain text.
        body_is_html=bool(body),
        to_addresses=tuple(
            _addresses(payload.get("toAddress")) + _addresses(payload.get("ccAddress"))
        ),
        thread_id=str(payload.get("threadId") or ""),
        rfc_message_id=str(payload.get("mailId") or payload.get("messageIdHeader") or ""),
        in_reply_to=in_reply_to,
        received_at=received,
        sent_at=sent,
        # Zoho reports mode/isDraft inconsistently across views; both spellings
        # are checked because a draft ingested as a reply would stop a sequence.
        is_draft=str(payload.get("mode") or "").lower() == "draft"
        or str(payload.get("isDraft") or "").lower() in {"true", "1"},
        removed=False,
        web_link="",
        has_attachments=str(payload.get("hasAttachment") or "").lower() in {"true", "1"},
    )


class ZohoSender:
    """EmailSender that sends as one connected Zoho mailbox.

    From is forced to the mailbox itself. Zoho would let a verified alias
    through, but the whole point of connecting a mailbox is that outreach goes
    out as that identity -- matching GraphSender, which cannot spoof at all.
    """

    name = "zoho"

    def __init__(self, db, connection: MailboxConnection, client: ZohoClient | None = None):
        self.connection = connection
        self.client = client or ZohoClient(db, connection)

    def send(self, email):
        from ..outreach.sender import SendResult

        body = {
            "fromAddress": self.connection.address,
            "toAddress": email.to,
            "subject": email.subject,
            "content": email.html_body or email.text_body,
            "mailFormat": "html",
            "encoding": "UTF-8",
        }
        # Threading hints, when we have a parent to thread off. Zoho documents
        # these on the draft API; the send API's handling is unverified, so a
        # provider that ignores them still delivers a fresh message rather than
        # rejecting the send.
        if email.in_reply_to:
            body["inReplyTo"] = email.in_reply_to
        if email.references:
            body["refHeader"] = email.references
        try:
            data = self.client.send_message(body)
        except (ZohoError, ValueError) as exc:
            logger.error("zoho send failed for %s: %s", self.connection.address, exc)
            return SendResult(ok=False, error=str(exc))

        native_id = str(data.get("messageId") or "")
        rfc_id = str(data.get("mailId") or "")
        if not native_id:
            # Without a native id nothing downstream can dedupe this message or
            # attach a reply draft to it, so say so instead of reporting a
            # success that quietly breaks reply detection.
            logger.error(
                "zoho send for %s returned no messageId: %s",
                self.connection.address,
                str(data)[:200],
            )
            return SendResult(ok=False, error="zoho send returned no messageId")
        return SendResult(
            ok=True,
            provider_message_id=rfc_id or native_id,
            provider_native_id=native_id,
            internet_message_id=rfc_id,
            conversation_id=str(data.get("threadId") or ""),
        )


def _cursor_state(cursor: str) -> tuple[int, set[str]]:
    """Decode a folder cursor into (high-water mark ms, ids seen at that ms).

    The id set exists because Zoho timestamps collide at millisecond
    granularity: a strict ``>`` would drop a co-timestamped sibling and a
    ``>=`` would replay one forever.
    """
    if not cursor:
        return 0, set()
    try:
        data = json.loads(cursor)
    except ValueError:
        return 0, set()
    return int(data.get("mark") or 0), set(data.get("seen") or [])


def _encode_cursor(mark: int, seen: set[str]) -> str:
    return json.dumps({"mark": mark, "seen": sorted(seen)})


class ZohoProvider:
    """MailProvider over the Zoho Mail REST API."""

    name = "zoho"

    def __init__(self, client_factory=None) -> None:
        self._client_factory = client_factory or ZohoClient

    def _client(self, db, connection: MailboxConnection) -> ZohoClient:
        return self._client_factory(db, connection)

    # ---- OAuth -----------------------------------------------------------

    def is_configured(self) -> bool:
        return is_configured()

    def redirect_uri(self) -> str:
        return redirect_uri()

    def authorize_url(self, state: str) -> str:
        return authorize_url(state)

    def exchange_code(self, code: str, params) -> ConnectResult:
        auth_base = resolve_auth_base(params)
        tokens = exchange_code(code, auth_base)
        api_base = api_base_for(auth_base)
        account = self._fetch_account(tokens["access_token"], api_base)
        folders = self._fetch_folder_ids(tokens["access_token"], api_base, account["id"])
        return ConnectResult(
            tokens=tokens,
            address=account["address"],
            display_name=account["display_name"],
            provider_account_id=account["id"],
            api_base=api_base,
            auth_base=auth_base,
            # Folder ids are stable and every later endpoint needs them, so
            # resolve once here rather than on every sync pass.
            sync_state={"folders": folders, "cursors": {}},
        )

    def _get(self, access_token: str, api_base: str, path: str):
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.get(
                f"{api_base.rstrip('/')}{path}",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
            )
        if response.status_code >= 400:
            raise ZohoError(
                f"zoho GET {path} failed ({response.status_code}): {response.text[:300]}",
                status_code=response.status_code,
            )
        payload = response.json()
        return payload.get("data", payload)

    def _fetch_account(self, access_token: str, api_base: str) -> dict:
        data = self._get(access_token, api_base, "/api/accounts")
        accounts = data if isinstance(data, list) else [data]
        if not accounts:
            raise ZohoError("no Zoho mail account on this login")
        account = accounts[0]
        address = (
            account.get("primaryEmailAddress")
            or account.get("mailboxAddress")
            or _one_address(account.get("emailAddress"))
        )
        if not address:
            raise ZohoError("Zoho account has no primary email address")
        return {
            "id": str(account.get("accountId") or ""),
            "address": address.lower(),
            "display_name": account.get("displayName") or account.get("accountName") or "",
        }

    def _fetch_folder_ids(self, access_token: str, api_base: str, account_id: str) -> dict:
        data = self._get(
            access_token, api_base, f"/api/accounts/{account_id}/folders"
        )
        folders = data if isinstance(data, list) else [data]
        wanted = {"inbox": FOLDER_INBOX, "sent": FOLDER_SENT, "drafts": _DRAFTS}
        found: dict[str, str] = {}
        for folder in folders:
            name = str(folder.get("folderName") or "").strip().lower()
            key = wanted.get(name)
            if key and folder.get("folderId"):
                found[key] = str(folder["folderId"])
        missing = [key for key in (FOLDER_INBOX, FOLDER_SENT) if key not in found]
        if missing:
            raise ZohoError(f"could not find Zoho folders: {', '.join(missing)}")
        return found

    # ---- sending ---------------------------------------------------------

    def build_sender(self, db, connection: MailboxConnection):
        return ZohoSender(db, connection, client=self._client(db, connection))

    # ---- reading ---------------------------------------------------------

    def _folder_id(self, connection: MailboxConnection, folder: str) -> str:
        folder_id = ((connection.sync_state or {}).get("folders") or {}).get(folder)
        if not folder_id:
            raise ZohoError(
                f"mailbox {connection.address} has no {folder} folder id; reconnect it"
            )
        return str(folder_id)

    def read_cursor(self, connection: MailboxConnection, folder: str) -> str:
        return ((connection.sync_state or {}).get("cursors") or {}).get(folder, "")

    def commit_cursor(
        self, db, connection: MailboxConnection, folder: str, cursor: str
    ) -> None:
        if not cursor:
            return
        # Reassign rather than mutate: SQLAlchemy will not notice an in-place
        # edit of a JSON dict, and a silently unsaved cursor means every pass
        # re-reads the same messages forever.
        state = dict(connection.sync_state or {})
        cursors = dict(state.get("cursors") or {})
        cursors[folder] = cursor
        state["cursors"] = cursors
        connection.sync_state = state

    def iter_folder(
        self, db, connection: MailboxConnection, folder: str, *, cursor: str
    ) -> FolderPage:
        client = self._client(db, connection)
        folder_id = self._folder_id(connection, folder)
        mark, seen_at_mark = _cursor_state(cursor)

        if not mark:
            # Baseline: record where "now" is from the newest message and read
            # nothing. Old mail is not outreach traffic.
            newest = client.list_folder(folder_id, start=1, limit=1)
            top = _epoch_ms(newest[0].get("receivedTime")) if newest else None
            mark_ms = int(top.timestamp() * 1000) if top else int(time.time() * 1000)
            ids = {str(newest[0].get("messageId"))} if newest else set()
            return FolderPage(next_cursor=_encode_cursor(mark_ms, ids), initialized=True)

        fresh: list[dict] = []
        reached_mark = False
        start = 1
        for _ in range(_MAX_CATCHUP_PAGES):
            page = client.list_folder(folder_id, start=start, limit=_PAGE_LIMIT)
            if not page:
                reached_mark = True
                break
            for payload in page:
                stamp = _epoch_ms(payload.get("receivedTime"))
                received_ms = int(stamp.timestamp() * 1000) if stamp else 0
                native_id = str(payload.get("messageId") or "")
                if received_ms < mark:
                    reached_mark = True
                    break
                if received_ms == mark and native_id in seen_at_mark:
                    reached_mark = True
                    break
                fresh.append(payload)
            if reached_mark or len(page) < _PAGE_LIMIT:
                reached_mark = True
                break
            start += _PAGE_LIMIT

        events = [self._hydrate(client, folder_id, payload) for payload in fresh]

        if not reached_mark:
            # We never met the old mark, so there is a gap between the oldest
            # message we fetched and it. Advancing now would skip that gap
            # permanently, so the mark stays put and the next pass carries on;
            # re-reading what we just ingested is free.
            logger.warning(
                "zoho catch-up for %s/%s exceeded %d pages; cursor held back",
                connection.address,
                folder,
                _MAX_CATCHUP_PAGES,
            )
            return FolderPage(events=events, next_cursor="")

        if not fresh:
            return FolderPage(events=[], next_cursor="")
        newest_ms = max(
            int(_epoch_ms(item.get("receivedTime")).timestamp() * 1000)
            for item in fresh
            if _epoch_ms(item.get("receivedTime")) is not None
        )
        ids_at_newest = {
            str(item.get("messageId") or "")
            for item in fresh
            if (stamp := _epoch_ms(item.get("receivedTime"))) is not None
            and int(stamp.timestamp() * 1000) == newest_ms
        }
        if newest_ms == mark:
            ids_at_newest |= seen_at_mark
        return FolderPage(events=events, next_cursor=_encode_cursor(newest_ms, ids_at_newest))

    def _hydrate(self, client: ZohoClient, folder_id: str, payload: dict) -> MailboxEvent:
        """Turn one list-view row into a full event.

        The list view carries a truncated summary, and intent classification on
        a snippet misreads long replies, so the body is fetched. Headers are a
        second call and are only worth it when there is no thread id to match
        on -- which is the only case where In-Reply-To buys anything.
        """
        native_id = str(payload.get("messageId") or "")
        body = ""
        try:
            content = client.fetch_content(folder_id, native_id)
            body = content.get("content") or content.get("body") or ""
        except ZohoError as exc:
            logger.warning("zoho content fetch failed for %s: %s", native_id, exc)

        in_reply_to = ""
        if not payload.get("threadId"):
            try:
                header = client.fetch_header(folder_id, native_id)
                in_reply_to = _header_value(header, "in-reply-to")
            except ZohoError as exc:
                logger.warning("zoho header fetch failed for %s: %s", native_id, exc)
        return to_event(payload, body=body, in_reply_to=in_reply_to)

    # ---- drafts and search ----------------------------------------------

    def place_reply_draft(self, db, *, connection, inbound, html_body: str) -> DraftResult:
        """Save the generated reply in the mailbox's own Drafts, on the thread.

        Zoho has no createReply-into-Drafts, so this is an ordinary draft with
        the threading headers set. Whether Zoho quotes the original for us once
        inReplyTo is present is unverified; if it does not, the draft still
        opens on the right thread with our text in it.
        """
        client = self._client(db, connection)
        body = {
            "fromAddress": connection.address,
            "toAddress": inbound.from_address or inbound.to_address,
            "subject": _reply_subject(inbound.subject),
            "content": html_body,
            "mailFormat": "html",
            "encoding": "UTF-8",
            "mode": "draft",
        }
        if inbound.internet_message_id:
            body["inReplyTo"] = inbound.internet_message_id
            body["refHeader"] = inbound.internet_message_id
        data = client.send_message(body)
        return DraftResult(provider_draft_id=str(data.get("messageId") or ""), web_link="")

    def search_participant(self, db, connection: MailboxConnection, address: str):
        client = self._client(db, connection)
        folder_id = self._folder_id(connection, FOLDER_INBOX)
        quoted = address.replace('"', "").strip()
        found = client.search(f"entire:{quoted}")
        return [self._hydrate(client, folder_id, payload) for payload in found]


def _header_value(header: dict, name: str) -> str:
    """Pull one header out of Zoho's header payload, whatever shape it took.

    The endpoint has returned both a flat mapping and a {"headers": {...}}
    envelope depending on the account, so both are handled rather than
    guessing.
    """
    if not header:
        return ""
    candidates = [header]
    nested = header.get("headers")
    if isinstance(nested, dict):
        candidates.append(nested)
    for source in candidates:
        for key, value in source.items():
            if str(key).lower() == name and isinstance(value, str):
                return value.strip()
    return ""


def _reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    if subject.lower().startswith("re:"):
        return subject
    return f"Re: {subject}" if subject else "Re:"

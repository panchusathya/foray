"""Seed inboxes connected with Google or Yahoo sign-in instead of an app password.

A brand-new Google account often cannot make an app password (it needs
2-Step Verification that has been on for a while), so a seed can be connected
with the provider's own consent screen instead. Read-only in both cases:

* **Google** -- ``gmail.readonly`` through the Gmail API. Labels say where a
  message landed (``SPAM``, ``INBOX`` + ``CATEGORY_PROMOTIONS`` ...). The
  consent goes through the Gmail callback already registered for sending
  mailboxes; the signed state says ``purpose: seed`` so that one callback can
  tell a seed from a mailbox (failure 23 in README.md). The scopes asked for are
  only the read scope -- never send or compose (failure 22).
* **Yahoo** -- Yahoo has no public mail REST API, so the token (``mail-r``) is
  used with the ordinary IMAP reader over SASL XOAUTH2. Yahoo needs its own
  registered redirect, ``/v1/deliverability/seeds/yahoo/callback``.

Tokens are stored encrypted on the seed. A refreshed access token is written
back, and a refresh or reconnect that returns no refresh token keeps the stored
one (failure 26). A refresh Google or Yahoo refuses is a read error on the seed
-- "reconnect it" -- never a placement (failure 25).

Google caveat worth knowing: while the Google OAuth app is in Testing mode,
Google expires its refresh tokens after seven days, so a seed connected that way
needs reconnecting weekly until the app is published (or is Internal to a
Workspace).
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from ..integrations.crypto import decrypt_json, encrypt_json
from .seeds import Placement, Probe, SeedReadError, _bare_id, _decode

GOOGLE_READ = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

YAHOO_AUTH_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_USERINFO_URL = "https://api.login.yahoo.com/openid/v1/userinfo"
YAHOO_IMAP = ("imap.mail.yahoo.com", 993)
YAHOO_READ = "mail-r"

_HTTP_TIMEOUT = 20.0
_SKEW = 60
_CLOCK_SLACK = timedelta(minutes=5)
_GMAIL_TABS = {
    "CATEGORY_PROMOTIONS": "promotions",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
}


class SeedOAuthError(Exception):
    """The provider refused a token request. Never carries a token."""


@dataclass(frozen=True)
class _Provider:
    name: str
    label: str
    provider_label: str  # SeedInbox.provider_label
    kind: str  # SeedInbox.kind
    credential_vars: str


PROVIDERS = {
    "google": _Provider(
        "google", "Google", "gmail", "gmail_api", "GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET"
    ),
    "yahoo": _Provider(
        "yahoo", "Yahoo", "yahoo", "imap_oauth", "YAHOO_CLIENT_ID and YAHOO_CLIENT_SECRET"
    ),
}


def is_configured(provider: str) -> bool:
    settings = get_settings()
    if provider == "google":
        return bool(settings.gmail_client_id and settings.gmail_client_secret)
    if provider == "yahoo":
        return bool(settings.yahoo_client_id and settings.yahoo_client_secret)
    return False


def redirect_uri(provider: str) -> str:
    if provider == "google":
        from ..integrations import gmail

        return gmail.redirect_uri()
    settings = get_settings()
    if settings.yahoo_seed_redirect_uri:
        return settings.yahoo_seed_redirect_uri
    return f"{settings.base_url.rstrip('/')}/v1/deliverability/seeds/yahoo/callback"


def authorize_url(provider: str, state: str) -> str:
    settings = get_settings()
    if provider == "google":
        from ..integrations import gmail

        query = {
            "client_id": settings.gmail_client_id,
            "redirect_uri": redirect_uri("google"),
            "response_type": "code",
            "scope": f"openid email {GOOGLE_READ}",
            "access_type": "offline",
            "prompt": "consent select_account",
            "include_granted_scopes": "false",
            "state": state,
        }
        return f"{gmail.AUTH_URL}?{urlencode(query)}"
    query = {
        "client_id": settings.yahoo_client_id,
        "redirect_uri": redirect_uri("yahoo"),
        "response_type": "code",
        "scope": f"openid email {YAHOO_READ}",
        "state": state,
    }
    return f"{YAHOO_AUTH_URL}?{urlencode(query)}"


# -- token requests (tests replace _exchange / _refresh) -------------------------


def _yahoo_token(grant: dict) -> dict:
    settings = get_settings()
    basic = base64.b64encode(
        f"{settings.yahoo_client_id}:{settings.yahoo_client_secret}".encode()
    ).decode()
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.post(
            YAHOO_TOKEN_URL,
            data={"redirect_uri": redirect_uri("yahoo"), **grant},
            headers={"Authorization": f"Basic {basic}"},
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400 or payload.get("error") or not payload.get("access_token"):
        detail = (
            payload.get("error_description")
            or payload.get("error")
            or f"HTTP {response.status_code}"
        )
        raise SeedOAuthError(f"Yahoo token request failed: {detail}")
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(payload.get("expires_in", 3600)),
        "scope": payload.get("scope", ""),
    }


def _userinfo_email(url: str, access_token: str) -> str:
    with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
        response = http.get(url, headers={"Authorization": f"Bearer {access_token}"})
    if response.status_code >= 400:
        raise SeedOAuthError(f"could not read the account's address (HTTP {response.status_code})")
    try:
        return str(response.json().get("email") or "").strip().lower()
    except ValueError:
        return ""


def _exchange(provider: str, code: str) -> tuple[str, dict]:
    """(the account's own address, tokens) for a consent callback's code."""
    if provider == "google":
        from ..integrations import gmail

        try:
            tokens = gmail.exchange_code(code)
        except gmail.GmailError as exc:
            raise SeedOAuthError(str(exc)) from None
        return _userinfo_email(gmail.USERINFO_URL, tokens["access_token"]), tokens
    tokens = _yahoo_token({"grant_type": "authorization_code", "code": code})
    return _userinfo_email(YAHOO_USERINFO_URL, tokens["access_token"]), tokens


def _refresh(provider: str, refresh_token: str) -> dict:
    if provider == "google":
        from ..integrations import gmail

        try:
            return gmail.refresh_tokens(refresh_token)
        except gmail.GmailError as exc:
            raise SeedOAuthError(str(exc)) from None
    return _yahoo_token({"grant_type": "refresh_token", "refresh_token": refresh_token})


def has_read_scope(provider: str, scope: str) -> bool:
    granted = set((scope or "").split())
    if provider == "google":
        return GOOGLE_READ in granted
    # Yahoo grants what the app is configured for and often omits the scope
    # from the response; an explicit list that lacks mail read is a refusal.
    return not granted or bool(granted & {YAHOO_READ, "mail-w"})


def _fresh_tokens(seed, provider: str, refresh, *, force: bool = False) -> dict:
    """The seed's tokens, refreshed (and written back) when expired or forced."""
    try:
        tokens = decrypt_json(seed.secret_blob) if seed.secret_blob else {}
    except ValueError:
        raise SeedReadError(
            f"The stored sign-in for {seed.address} could not be decrypted: reconnect this seed"
        ) from None
    if (
        not force
        and tokens.get("access_token")
        and int(tokens.get("expires_at") or 0) - _SKEW > time.time()
    ):
        return tokens
    refresh_token = tokens.get("refresh_token") or ""
    if not refresh_token:
        raise SeedReadError(f"No stored sign-in for {seed.address}: reconnect this seed")
    label = PROVIDERS[provider].label
    try:
        new = refresh(provider, refresh_token)
    except SeedOAuthError:
        raise SeedReadError(
            f"{label} refused the stored sign-in for {seed.address} "
            "(expired or revoked): reconnect this seed"
        ) from None
    merged = {**tokens, **{k: v for k, v in new.items() if v}}
    merged["refresh_token"] = new.get("refresh_token") or refresh_token
    seed.secret_blob = encrypt_json(merged)
    return merged


def reader_for_oauth_seed(seed):
    if seed.kind == "gmail_api":
        return GmailApiReader(seed)
    from .seeds import ImapReader

    tokens = _fresh_tokens(seed, "yahoo", lambda p, t: _refresh(p, t))
    return ImapReader(
        host=seed.imap_host or YAHOO_IMAP[0],
        port=seed.imap_port or YAHOO_IMAP[1],
        username=seed.username or seed.address,
        access_token=tokens["access_token"],
    )


# -- the Gmail API reader -------------------------------------------------------


def _http_transport():
    client = httpx.Client(timeout=_HTTP_TIMEOUT)

    def transport(method, url, params, token):
        try:
            response = client.request(
                method, url, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise SeedReadError(f"Could not reach Gmail ({type(exc).__name__})") from None
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body

    transport.close = client.close
    return transport


class GmailApiReader:
    """Same contract as ImapReader: a context manager with ``find(probe)``."""

    spam_visible = True

    def __init__(self, seed, *, transport=None, refresh=None) -> None:
        self.seed = seed
        self._transport = transport
        self._own_transport = transport is None
        self._refresh = refresh or (lambda provider, token: _refresh(provider, token))
        self._token = ""

    def __enter__(self) -> GmailApiReader:
        self._token = _fresh_tokens(self.seed, "google", self._refresh)["access_token"]
        if self._transport is None:
            self._transport = _http_transport()
        return self

    def __exit__(self, *exc) -> bool:
        if self._own_transport and self._transport is not None:
            getattr(self._transport, "close", lambda: None)()
            self._transport = None
        return False

    def _get(self, path: str, params: dict) -> dict:
        status, body = self._transport("GET", f"{GMAIL_API}{path}", params, self._token)
        if status == 401:
            self._token = _fresh_tokens(self.seed, "google", self._refresh, force=True)[
                "access_token"
            ]
            status, body = self._transport("GET", f"{GMAIL_API}{path}", params, self._token)
        if status >= 400:
            raise SeedReadError(f"Gmail refused the read for {self.seed.address} (HTTP {status})")
        return body or {}

    def _search(self, query: str, limit: int) -> list[str]:
        body = self._get("/messages", {"q": query, "includeSpamTrash": "true", "maxResults": limit})
        return [str(m.get("id")) for m in body.get("messages") or [] if m.get("id")]

    def _meta(self, message_id: str) -> dict:
        return self._get(
            f"/messages/{message_id}",
            {"format": "metadata", "metadataHeaders": ["From", "Subject", "Message-ID"]},
        )

    @staticmethod
    def _placement(labels: set[str]) -> Placement:
        if "SPAM" in labels:
            return Placement(found=True, placement="spam", folder="Spam")
        if "INBOX" in labels:
            for label, tab in _GMAIL_TABS.items():
                if label in labels:
                    return Placement(found=True, placement="other_tab", folder=tab)
            return Placement(found=True, placement="inbox", folder="INBOX")
        # Delivered but out of the inbox: archived by a filter, or trashed.
        folder = "trash" if "TRASH" in labels else "archived"
        return Placement(found=True, placement="other_tab", folder=folder)

    def find(self, probe: Probe) -> Placement:
        bare = _bare_id(probe.message_id)
        if bare:
            for message_id in self._search(f"rfc822msgid:{bare}", 5):
                meta = self._meta(message_id)
                return self._placement(set(meta.get("labelIds") or []))
        floor = (probe.not_before - _CLOCK_SLACK) if probe.not_before else None
        query = f"from:{probe.from_address}"
        if floor is not None:
            query += f" after:{int(floor.timestamp())}"
        want_subject = _decode(probe.subject)
        want_from = (probe.from_address or "").strip().lower()
        from email.utils import parseaddr

        for message_id in self._search(query, 25):
            meta = self._meta(message_id)
            headers = {
                str(h.get("name") or "").lower(): str(h.get("value") or "")
                for h in (meta.get("payload") or {}).get("headers") or []
            }
            if parseaddr(headers.get("from", ""))[1].strip().lower() != want_from:
                continue
            if _decode(headers.get("subject", "")) != want_subject:
                continue
            try:
                arrived = int(meta.get("internalDate") or 0) / 1000
            except (TypeError, ValueError):
                arrived = 0
            if floor is not None and arrived and arrived < floor.timestamp():
                continue
            return self._placement(set(meta.get("labelIds") or []))
        return Placement(found=False)


# -- finishing a connect ----------------------------------------------------------


def complete_connect(db, *, provider: str, org_id: str, code: str) -> tuple[bool, str, str]:
    """Turn a consent callback into a seed. (ok, page title, page body)."""
    from sqlalchemy import select

    from ..models import MailboxConnection, SeedInbox, record_event

    meta = PROVIDERS[provider]
    try:
        address, tokens = _exchange(provider, code)
    except SeedOAuthError as exc:
        return False, "Connection failed", str(exc)
    if not address:
        return False, "Connection failed", f"{meta.label} did not say which address this is."
    if not has_read_scope(provider, tokens.get("scope", "")):
        return (
            False,
            "Read access was not granted",
            f"The seed needs permission to read mail. Connect again and leave the "
            f"{meta.label} mail read box ticked.",
        )
    address = address.strip().lower()
    sending = db.scalar(
        select(MailboxConnection.id).where(
            MailboxConnection.org_id == org_id,
            MailboxConnection.address == address,
            MailboxConnection.status != "revoked",
        )
    )
    if sending:
        return (
            False,
            "That is a sending mailbox",
            f"{address} sends outreach. A seed has to be an inbox you only receive on.",
        )
    seed = db.scalar(
        select(SeedInbox).where(SeedInbox.org_id == org_id, SeedInbox.address == address)
    )
    if seed is None:
        seed = SeedInbox(org_id=org_id, address=address)
        db.add(seed)
        stored = {}
    else:
        try:
            stored = decrypt_json(seed.secret_blob) if seed.secret_blob else {}
        except ValueError:
            stored = {}
    if not tokens.get("refresh_token") and stored.get("refresh_token"):
        tokens = {**tokens, "refresh_token": stored["refresh_token"]}
    seed.kind = meta.kind
    seed.provider_label = meta.provider_label
    seed.username = address
    if provider == "yahoo":
        seed.imap_host, seed.imap_port = YAHOO_IMAP
    else:
        seed.imap_host, seed.imap_port = "", 993
    seed.secret_blob = encrypt_json(tokens)
    seed.status = "active"
    seed.error = ""
    seed.spam_visible = True
    db.flush()
    record_event(
        db,
        org_id=org_id,
        type="deliverability.seed_added",
        subject_type="seed_inbox",
        subject_id=seed.id,
        payload={"address": address, "provider": meta.provider_label, "via": "oauth"},
    )
    return (
        True,
        "Seed inbox connected",
        f"{address} is now a seed inbox. Tests sent to it are read from {meta.label}. "
        "You can close this tab.",
    )

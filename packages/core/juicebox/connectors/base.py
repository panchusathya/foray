"""The connector protocol, its DTOs, and the registry.

The DTOs are the privacy contract, the same way ``SweepRecord`` is for mailbox
discovery: there is **no field to put message content in**. A connector can
report that an attachment exists and fetch its bytes; it cannot hand back a
subject line or a body, because nothing downstream has a slot for one. A test
pins this the way ``DiscoveredContact``'s absence-of-columns is pinned.

Registry idioms copied from ``messaging.channels``: lazy defaults, explicit
``set_connector`` for tests, an unknown name is a hard error. Every default
answers ``is_configured() == False`` until its OAuth credentials are set, and
the conversation only offers connectors that are configured — so with no
credentials the feature does not exist, link and all.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..integrations.crypto import decrypt_json, encrypt_json


class ConnectorError(RuntimeError):
    """Any connector-side failure. Safe for logs, never shown to a candidate."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class ConnectorGrant:
    """What the OAuth callback persists for one connected app.

    ``tokens`` goes straight into ``encrypt_json`` and is never read outside
    the provider module that wrote it. ``address`` is the provider-verified
    identity (an email for mail providers, a login for GitHub) — stronger than
    anything typed into a chat, which is why the callback may fill an empty
    ``contact_email`` from it but never overwrite a stated one.
    """

    tokens: dict[str, Any]
    address: str
    display_name: str = ""
    provider_account_id: str = ""
    scopes: str = ""


@dataclass(frozen=True)
class ResumeHit:
    """One attachment worth considering. Metadata only, structurally.

    ``native_id`` is whatever the provider needs to fetch the bytes later
    (message id + attachment id, joined however the provider likes) and doubles
    as the idempotency handle in ``index_state``. ``sent_by_owner`` is the
    strongest resume signal there is: a file they attached themselves beats
    any file somebody sent them.
    """

    native_id: str
    filename: str
    content_type: str = ""
    sent_by_owner: bool = False
    occurred_at: datetime | None = None
    size_bytes: int = 0


@dataclass(frozen=True)
class ConnectorProfile:
    """Derived facts about the person, in the vocabulary the flow already
    speaks (``LinkedInProfile``'s fields, plus the two connector extras).

    ``skills`` are things they demonstrably HAVE (GitHub languages), never
    requirements they want — the distinction ``extract`` enforces when this
    reaches the record.
    """

    full_name: str = ""
    current_title: str = ""
    current_employer: str = ""
    location: str = ""
    experience_years: float | None = None
    headline: str = ""
    skills: tuple[str, ...] = ()
    linkedin_url: str = ""
    github_url: str = ""


class CandidateConnector(Protocol):
    """One connectable app. OAuth plus a handful of targeted reads.

    Providers own their wire formats end to end — nothing outside the module
    sees a Gmail part tree or a Graph ``$select`` — and expose only the DTOs
    above. ``list_resume_hits`` runs the *targeted* queries only; a connector
    that crawls a mailbox does not belong behind this protocol.
    """

    name: str
    label: str

    def is_configured(self) -> bool: ...

    def redirect_uri(self) -> str: ...

    def authorize_url(self, state: str) -> str: ...

    def exchange_code(self, code: str, params: Mapping[str, str]) -> ConnectorGrant: ...

    def list_resume_hits(self, db, connection) -> list[ResumeHit]: ...

    def fetch_resume(self, db, connection, hit: ResumeHit) -> bytes: ...

    def fetch_profile(self, db, connection) -> ConnectorProfile: ...

    def revoke(self, db, connection) -> None: ...


# --------------------------------------------------------------------------
# shared token plumbing
# --------------------------------------------------------------------------

_TOKEN_SKEW_SECONDS = 60


def fresh_access_token(db, connection, *, refresh) -> str:
    """Decrypt, refresh when near expiry, re-encrypt. The ``GraphClient``
    pattern, shared here so three providers do not re-learn its two lessons:
    a rotated-away refresh token means keep the old one, and a failed refresh
    marks the connection ``error`` instead of crashing every later call the
    same way.

    ``refresh`` is the provider's own ``refresh_tokens(refresh_token) -> dict``
    returning the normalised ``{access_token, refresh_token, expires_at}``.
    """
    tokens = decrypt_json(connection.token_blob)
    if tokens.get("expires_at", 0) - _TOKEN_SKEW_SECONDS > time.time():
        return tokens["access_token"]
    try:
        fresh = refresh(tokens.get("refresh_token", ""))
    except ConnectorError as exc:
        connection.status = "error"
        connection.error = str(exc)[:400]
        db.flush()
        raise
    if not fresh.get("refresh_token"):
        fresh["refresh_token"] = tokens.get("refresh_token", "")
    connection.token_blob = encrypt_json(fresh)
    connection.status = "active"
    connection.error = ""
    db.flush()
    return fresh["access_token"]


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

_connectors: dict[str, CandidateConnector] = {}


def _load_defaults() -> None:
    from ..config import get_settings
    from .github import GithubConnector
    from .google import GoogleConnector
    from .microsoft import MicrosoftCandidateConnector

    for connector in (GoogleConnector(), MicrosoftCandidateConnector(), GithubConnector()):
        _connectors.setdefault(connector.name, connector)
    # The fake is dev-only and opt-in by env flag; production never registers
    # it by default, so a stray flag on a laptop cannot ship a toy connector.
    if get_settings().fake_connector_enabled:
        from .fake import FakeConnector

        _connectors.setdefault("fake", FakeConnector())


def get_connector(name: str) -> CandidateConnector:
    """Look up a connector by name. Unknown is a hard error, never a fallback:
    silently defaulting would OAuth against the wrong provider."""
    if not _connectors:
        _load_defaults()
    try:
        return _connectors[name]
    except KeyError:
        raise ConnectorError(f"unknown connector {name!r}") from None


def connector_names() -> list[str]:
    if not _connectors:
        _load_defaults()
    return sorted(_connectors)


def configured_connectors() -> list[CandidateConnector]:
    """The connectors the conversation may offer, in a stable order."""
    if not _connectors:
        _load_defaults()
    return [
        _connectors[name] for name in sorted(_connectors) if _connectors[name].is_configured()
    ]


def set_connector(name: str, connector: CandidateConnector | None) -> None:
    """Install or remove a connector. Tests use this to inject ``FakeConnector``."""
    if not _connectors:
        _load_defaults()
    if connector is None:
        _connectors.pop(name, None)
        return
    _connectors[name] = connector


def reset_connectors() -> None:
    """Drop every override. Called between tests so one cannot leak forward."""
    _connectors.clear()


def field_names(dto: type) -> tuple[str, ...]:
    """The DTO's field names, for the structural no-content test."""
    return tuple(getattr(dto, "__dataclass_fields__", {}).keys())


__all__ = [
    "CandidateConnector",
    "ConnectorError",
    "ConnectorGrant",
    "ConnectorProfile",
    "ResumeHit",
    "configured_connectors",
    "connector_names",
    "field_names",
    "fresh_access_token",
    "get_connector",
    "reset_connectors",
    "set_connector",
]

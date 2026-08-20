"""The one place Juicebox talks to Foray.

Foray is a separate service with its own database and no notion of an
organisation, so it cannot authorise anything. Juicebox can: it has principals,
orgs and write permissions. That asymmetry decides the shape here -- Juicebox
authenticates the human, then calls Foray over the Docker network with a shared
secret, and Foray's paid routes trust that secret and nothing else.

Kept deliberately thin. The browser already talks to Foray directly through the
nginx ``/foray/`` proxy for search and enrichment, which are free and read-only;
only the paid path needs to come through here, so this is not the beginning of a
general-purpose client.
"""

from __future__ import annotations

import logging

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class ForayError(RuntimeError):
    """Foray was unreachable, misconfigured, or refused the call."""


class ForayClient:
    """Calls Foray's internal, shared-secret routes."""

    def __init__(self, *, base_url: str | None = None, token: str | None = None) -> None:
        settings = get_settings()
        self._base_url = (base_url or settings.foray_base_url).rstrip("/")
        self._token = token if token is not None else settings.foray_internal_token
        self._timeout = settings.foray_timeout_seconds

    def is_configured(self) -> bool:
        return bool(self._base_url and self._token)

    async def find_emails(self, logins: list[str], reason: str) -> dict:
        """Ask Foray to buy contact details for these candidates.

        Foray applies the spend discipline (already-checked, already-has-one, no
        profile to match), so this does not try to second-guess which logins are
        worth sending -- doing that in two places is how the two drift apart.
        """
        if not self.is_configured():
            raise ForayError(
                "Foray is not configured: set FORAY_BASE_URL and FORAY_INTERNAL_TOKEN"
            )
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    f"{self._base_url}/internal/candidates/find-emails",
                    headers={"X-Foray-Token": self._token},
                    json={"logins": logins, "reason": reason},
                )
        except httpx.HTTPError as exc:
            raise ForayError(f"Foray is unreachable: {exc}") from exc

        if response.status_code >= 400:
            # Pass the reason through rather than a bare status: "CONTACTOUT_API_KEY
            # is not configured" is actionable and a 503 alone is not.
            detail = _detail(response)
            log.warning("foray.find_emails failed status=%s detail=%s", response.status_code, detail)
            raise ForayError(detail)
        return response.json()


def _detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200] or f"Foray returned {response.status_code}"
    if isinstance(payload, dict) and payload.get("detail"):
        return str(payload["detail"])
    return f"Foray returned {response.status_code}"


_client: ForayClient | None = None


def get_foray() -> ForayClient:
    """The process-wide client. Built lazily so importing this module never
    requires the settings to be complete."""
    global _client
    if _client is None:
        _client = ForayClient()
    return _client


def set_foray(client: ForayClient | None) -> None:
    """Tests inject a recorder; nothing in production calls this."""
    global _client
    _client = client


def reset_foray() -> None:
    global _client
    _client = None

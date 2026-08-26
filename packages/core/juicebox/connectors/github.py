"""GitHub, read-only, for what a candidate's public work says they can do.

An OAuth *web-application* flow (candidate consents in a browser), unrelated
to the ``GITHUB_TOKEN`` PAT the sourcing pipelines use. ``read:user`` is the
only scope — public repos are readable with any token, and nothing here needs
more.

The extraction heuristics are ported from
``packages/ingest/src/connectors/github.js`` so both GitHub readers agree on
what a profile means: forks don't count, a language's weight is its repo
count plus stars, the top eight languages are the skill list. Languages are
*haves*, and ``connectors.extract`` keeps them out of ``spec`` — they seed the
candidate's skill haystack, never the role requirements.

GitHub OAuth-app tokens do not expire, so refresh is a stored no-op; the
grant is deleted upstream on disconnect instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ..config import get_settings
from .base import ConnectorError, ConnectorGrant, ConnectorProfile, ResumeHit, fresh_access_token

AUTH_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_BASE = "https://api.github.com"
SCOPES = "read:user"

_HTTP_TIMEOUT = 30.0
#: Effectively never: GitHub OAuth-app tokens have no expiry, and storing a
#: real timestamp keeps the shared token helper's contract intact.
_NO_EXPIRY = 2**33
_MAX_LANGUAGES = 8
_API_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _fail(response: httpx.Response, doing: str) -> ConnectorError:
    return ConnectorError(
        f"{doing} failed: {response.status_code} {response.text[:200]}",
        status_code=response.status_code,
    )


def languages_from_repos(repos: list[dict]) -> list[str]:
    """Fork-free language tally, weighted by stars. The ingest heuristic,
    verbatim in spirit: `1 + stargazers_count` per repo, top eight."""
    tally: dict[str, int] = {}
    for repo in repos:
        language = repo.get("language")
        if repo.get("fork") or not language:
            continue
        tally[language] = tally.get(language, 0) + 1 + int(repo.get("stargazers_count") or 0)
    ranked = sorted(tally.items(), key=lambda item: -item[1])
    return [language for language, _ in ranked[:_MAX_LANGUAGES]]


class GithubConnector:
    name = "github"
    label = "GitHub"

    def is_configured(self) -> bool:
        settings = get_settings()
        return bool(settings.github_oauth_client_id and settings.github_oauth_client_secret)

    def redirect_uri(self) -> str:
        settings = get_settings()
        if settings.github_oauth_redirect_uri:
            return settings.github_oauth_redirect_uri
        return f"{settings.base_url.rstrip('/')}/v1/connect/github/callback"

    def authorize_url(self, state: str) -> str:
        settings = get_settings()
        query = urlencode(
            {
                "client_id": settings.github_oauth_client_id,
                "redirect_uri": self.redirect_uri(),
                "scope": SCOPES,
                "state": state,
            }
        )
        return f"{AUTH_URL}?{query}"

    def refresh_tokens(self, refresh_token: str) -> dict:
        raise ConnectorError("github tokens do not refresh; reconnect required")

    def exchange_code(self, code: str, params: Mapping[str, str]) -> ConnectorGrant:
        settings = get_settings()
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.post(
                TOKEN_URL,
                data={
                    "client_id": settings.github_oauth_client_id,
                    "client_secret": settings.github_oauth_client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri(),
                },
                headers={"Accept": "application/json"},
            )
        if response.status_code >= 400:
            raise _fail(response, "token request")
        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise ConnectorError(
                f"github refused the code: {payload.get('error_description') or payload.get('error', 'no token returned')}"
            )
        tokens = {
            "access_token": access_token,
            "refresh_token": "",
            "expires_at": _NO_EXPIRY,
        }
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            me = http.get(
                f"{API_BASE}/user",
                headers={**_API_HEADERS, "Authorization": f"Bearer {access_token}"},
            )
        if me.status_code >= 400:
            raise _fail(me, "profile request")
        user = me.json()
        login = str(user.get("login") or "")
        if not login:
            raise ConnectorError("github returned no login")
        return ConnectorGrant(
            tokens=tokens,
            address=login,
            display_name=str(user.get("name") or login),
            provider_account_id=str(user.get("id") or login),
            scopes=SCOPES,
        )

    # -- reads ------------------------------------------------------------

    def _get(self, db, connection, path: str, params: dict | None = None) -> dict | list:
        token = fresh_access_token(db, connection, refresh=self.refresh_tokens)
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            response = http.get(
                f"{API_BASE}{path}",
                params=params,
                headers={**_API_HEADERS, "Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            raise _fail(response, f"github GET {path}")
        return response.json()

    def list_resume_hits(self, db, connection) -> list[ResumeHit]:
        """GitHub carries no documents; the profile path is the whole value."""
        return []

    def fetch_resume(self, db, connection, hit: ResumeHit) -> bytes:
        raise ConnectorError("github has no resume attachments")

    def fetch_profile(self, db, connection) -> ConnectorProfile:
        user = self._get(db, connection, "/user")
        login = str(user.get("login") or "")
        repos = self._get(
            db, connection, f"/users/{login}/repos", {"per_page": 100, "sort": "pushed"}
        )
        repos = repos if isinstance(repos, list) else []
        bio = str(user.get("bio") or "").strip()
        return ConnectorProfile(
            full_name=str(user.get("name") or login).strip(),
            # A bio is not a job title; leaving this empty routes the target
            # question to gap-fill instead of guessing a search off a tagline.
            current_title="",
            current_employer=str(user.get("company") or "").lstrip("@").strip(),
            location=str(user.get("location") or "").strip(),
            headline=bio[:300],
            skills=tuple(languages_from_repos(repos)),
            github_url=str(user.get("html_url") or ""),
        )

    def revoke(self, db, connection) -> None:
        """Delete the OAuth grant so the app disappears from their settings."""
        from ..integrations.crypto import decrypt_json

        settings = get_settings()
        try:
            tokens = decrypt_json(connection.token_blob)
        except Exception:  # noqa: BLE001 - nothing stored, nothing to revoke
            return
        token = tokens.get("access_token") or ""
        if not token or not self.is_configured():
            return
        with httpx.Client(timeout=_HTTP_TIMEOUT) as http:
            http.request(
                "DELETE",
                f"{API_BASE}/applications/{settings.github_oauth_client_id}/grant",
                auth=(settings.github_oauth_client_id, settings.github_oauth_client_secret),
                headers=_API_HEADERS,
                json={"access_token": token},
            )

"""Browser Use Cloud session lifecycle.

Live apply drives a managed Chrome over CDP. This module is the only place
that talks to Browser Use: create a session, hand back ``cdpUrl``, GET it
again after a crash, and always ``PATCH`` stop. Playwright ``browser.close()``
does not end billing -- the docs are explicit -- so every path that creates
must stop in ``finally``.

The Cloud SDK's browser resource currently lives in the v3 namespace while
the REST contract is API v4 (``https://api.browser-use.com/api/v4``). We
wrap the SDK when it is installed and fall back to the documented REST
shape so tests can inject a fake without the package. Hosted agent runs
(``POST /runs``) are out of scope: Juicebox keeps the planner.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ....config import get_settings

logger = logging.getLogger(__name__)

API_BASE = "https://api.browser-use.com/api/v4"
DEFAULT_TIMEOUT_MIN = 15
MAX_TIMEOUT_MIN = 240


class CloudError(RuntimeError):
    """A Browser Use call failed in a way the submitter must see."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TooManySessions(CloudError):
    """Plan concurrency cap. Requeue; do not burn an application attempt."""


class ProfileQuota(CloudError):
    """Plan profile cap. Hand off; never silently share a profile across people."""


@dataclass(frozen=True)
class CloudSession:
    id: str
    cdp_url: str = ""
    live_url: str = ""
    status: str = "active"
    profile_id: str = ""


class CloudClient(Protocol):
    def create_browser(self, payload: dict[str, Any]) -> CloudSession: ...
    def get_browser(self, session_id: str) -> CloudSession | None: ...
    def stop_browser(self, session_id: str) -> None: ...
    def create_profile(self, *, name: str, user_id: str) -> str: ...
    def find_profile(self, user_id: str) -> str | None: ...


_override: CloudClient | None = None


def set_cloud_client(client: CloudClient | None) -> None:
    """Tests pin a fake; ``None`` restores the real client."""
    global _override
    _override = client


def configured() -> bool:
    """A client can create sessions: a real key, or a test fake."""
    if _override is not None:
        return True
    return bool((get_settings().browser_use_api_key or "").strip())


def agent_configured() -> bool:
    """Product switch: key + enable + the data-processing acknowledgement."""
    settings = get_settings()
    return bool(configured() and settings.apply_browser_agent and settings.apply_browser_agent_ack)


def _client() -> CloudClient:
    if _override is not None:
        return _override
    key = (get_settings().browser_use_api_key or "").strip()
    if not key:
        raise CloudError("BROWSER_USE_API_KEY is not set")
    return _SdkOrRestClient(api_key=key)


def create_session(
    *,
    profile_id: str = "",
    metadata: dict[str, str] | None = None,
    timeout_min: int | None = None,
) -> CloudSession:
    settings = get_settings()
    timeout = timeout_min if timeout_min is not None else settings.apply_browser_agent_timeout_min
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT_MIN), MAX_TIMEOUT_MIN))
    country = (settings.apply_browser_proxy_country or "us").strip().lower() or "us"
    payload: dict[str, Any] = {
        "proxyCountryCode": country,
        "timeout": timeout,
        "enableRecording": bool(settings.apply_browser_agent_recording),
    }
    if profile_id:
        payload["profileId"] = profile_id
    if metadata:
        payload["metadata"] = {str(k): str(v)[:200] for k, v in list(metadata.items())[:10]}
    return _client().create_browser(payload)


def get_session(session_id: str) -> CloudSession | None:
    if not session_id:
        return None
    try:
        return _client().get_browser(session_id)
    except CloudError as exc:
        logger.warning("could not GET browser session %s: %s", session_id, exc)
        return None


def stop_session(session_id: str) -> None:
    """Idempotent. A missing or already-stopped session is not an error."""
    if not session_id:
        return
    try:
        _client().stop_browser(session_id)
    except CloudError as exc:
        logger.warning("could not stop browser session %s: %s", session_id, exc)


def create_profile(*, name: str, user_id: str) -> str:
    return _client().create_profile(name=name[:100], user_id=user_id[:255])


def find_profile(user_id: str) -> str | None:
    return _client().find_profile(user_id[:255])


def _session_from(data: Any) -> CloudSession:
    if not isinstance(data, dict):
        data = {
            "id": getattr(data, "id", ""),
            "cdpUrl": getattr(data, "cdp_url", None) or getattr(data, "cdpUrl", ""),
            "liveUrl": getattr(data, "live_url", None) or getattr(data, "liveUrl", ""),
            "status": getattr(data, "status", "active"),
            "profileId": getattr(data, "profile_id", None) or getattr(data, "profileId", ""),
        }
    return CloudSession(
        id=str(data.get("id") or ""),
        cdp_url=str(data.get("cdpUrl") or data.get("cdp_url") or ""),
        live_url=str(data.get("liveUrl") or data.get("live_url") or ""),
        status=str(data.get("status") or "active"),
        profile_id=str(data.get("profileId") or data.get("profile_id") or ""),
    )


class _SdkOrRestClient:
    """SDK when importable, otherwise the documented v4 REST endpoints."""

    def __init__(self, *, api_key: str) -> None:
        self._api_key = api_key
        self._sdk = None
        try:
            from browser_use_sdk.v3 import BrowserUse

            self._sdk = BrowserUse(api_key=api_key)
        except Exception:  # noqa: BLE001 - missing extra, or ctor shape drifted
            self._sdk = None

    def create_browser(self, payload: dict[str, Any]) -> CloudSession:
        if self._sdk is not None:
            try:
                browser = self._sdk.browsers.create(
                    proxy_country_code=payload.get("proxyCountryCode"),
                    timeout=payload.get("timeout"),
                    profile_id=payload.get("profileId"),
                    enable_recording=payload.get("enableRecording"),
                    metadata=payload.get("metadata"),
                )
                session = _session_from(browser)
                if session.id and session.cdp_url:
                    return session
            except Exception as exc:  # noqa: BLE001
                self._raise_sdk(exc)
        return _session_from(self._request("POST", "/browsers", json=payload, expected=(201, 200)))

    def get_browser(self, session_id: str) -> CloudSession | None:
        if self._sdk is not None:
            for name in ("retrieve", "get"):
                method = getattr(getattr(self._sdk, "browsers", None), name, None)
                if callable(method):
                    try:
                        return _session_from(method(session_id))
                    except Exception:  # noqa: BLE001
                        break
        data = self._request("GET", f"/browsers/{session_id}", expected=(200,), allow_404=True)
        return None if data is None else _session_from(data)

    def stop_browser(self, session_id: str) -> None:
        if self._sdk is not None:
            stop = getattr(getattr(self._sdk, "browsers", None), "stop", None)
            if callable(stop):
                try:
                    stop(session_id)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.info("SDK stop fell through to REST: %s", exc)
        self._request(
            "PATCH",
            f"/browsers/{session_id}",
            json={"action": "stop"},
            expected=(200, 404),
            allow_404=True,
        )

    def create_profile(self, *, name: str, user_id: str) -> str:
        if self._sdk is not None:
            create = getattr(getattr(self._sdk, "profiles", None), "create", None)
            if callable(create):
                try:
                    row = create(name=name, user_id=user_id)
                    pid = str(getattr(row, "id", None) or (row.get("id") if isinstance(row, dict) else "") or "")
                    if pid:
                        return pid
                except Exception as exc:  # noqa: BLE001
                    self._raise_sdk(exc)
        data = self._request(
            "POST", "/profiles", json={"name": name, "userId": user_id}, expected=(201, 200)
        )
        pid = str((data or {}).get("id") or "")
        if not pid:
            raise CloudError("profile create returned no id")
        return pid

    def find_profile(self, user_id: str) -> str | None:
        data = self._request(
            "GET",
            "/profiles",
            params={"userId": user_id},
            expected=(200,),
            allow_404=True,
        )
        if data is None:
            return None
        rows = data if isinstance(data, list) else data.get("items") or data.get("profiles") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("userId") or row.get("user_id") or "") == user_id:
                pid = str(row.get("id") or "")
                if pid:
                    return pid
        return None

    def _raise_sdk(self, exc: Exception) -> None:
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            raise TooManySessions("too many concurrent Browser Use sessions") from exc
        if status == 402:
            raise ProfileQuota("Browser Use profile quota reached") from exc
        raise CloudError(str(exc)[:400], status_code=status) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        expected: tuple[int, ...],
        allow_404: bool = False,
    ) -> dict | list | None:
        import httpx

        try:
            response = httpx.request(
                method,
                API_BASE + path,
                headers={
                    "X-Browser-Use-API-Key": self._api_key,
                    "Content-Type": "application/json",
                },
                json=json,
                params=params,
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise CloudError(f"Browser Use request failed: {exc}") from exc
        if response.status_code == 429:
            raise TooManySessions("too many concurrent Browser Use sessions")
        if response.status_code == 402 and path.startswith("/profiles"):
            raise ProfileQuota("Browser Use profile quota reached")
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code not in expected:
            raise CloudError(
                f"Browser Use {method} {path} returned {response.status_code}: {response.text[:200]}",
                status_code=response.status_code,
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise CloudError("Browser Use returned non-JSON") from exc


class FakeCloudClient:
    """In-memory Browser Use stand-in. Tests pin it with ``set_cloud_client``."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.browsers: dict[str, CloudSession] = {}
        self.profiles: dict[str, str] = {}
        self.fail_create: Exception | None = None
        self.fail_profile: Exception | None = None
        self._n = 0

    def create_browser(self, payload: dict[str, Any]) -> CloudSession:
        if self.fail_create is not None:
            raise self.fail_create
        self._n += 1
        session_id = f"bu-{self._n}"
        session = CloudSession(
            id=session_id,
            cdp_url=f"ws://browser-use.test/{session_id}",
            live_url=f"https://live.browser-use.test/{session_id}",
            status="active",
            profile_id=str(payload.get("profileId") or ""),
        )
        self.created.append(dict(payload))
        self.browsers[session_id] = session
        return session

    def get_browser(self, session_id: str) -> CloudSession | None:
        return self.browsers.get(session_id)

    def stop_browser(self, session_id: str) -> None:
        self.stopped.append(session_id)
        held = self.browsers.get(session_id)
        if held is not None:
            self.browsers[session_id] = CloudSession(
                id=held.id,
                cdp_url=held.cdp_url,
                live_url=held.live_url,
                status="stopped",
                profile_id=held.profile_id,
            )

    def create_profile(self, *, name: str, user_id: str) -> str:
        if self.fail_profile is not None:
            raise self.fail_profile
        existing = self.profiles.get(user_id)
        if existing:
            return existing
        self._n += 1
        pid = f"prof-{self._n}"
        self.profiles[user_id] = pid
        return pid

    def find_profile(self, user_id: str) -> str | None:
        return self.profiles.get(user_id)

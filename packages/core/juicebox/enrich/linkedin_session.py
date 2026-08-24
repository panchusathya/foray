"""Load a LinkedIn session for the browser scraper.

Credentials never live in the repo. Ops paste a cookie JSON (Playwright
``storage_state`` cookies array, or a raw ``li_at=...`` string) into
``LINKEDIN_SESSION_COOKIE`` or a file at ``LINKEDIN_SESSION_PATH``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..config import get_settings

logger = logging.getLogger(__name__)

_LINKEDIN_DOMAIN = ".linkedin.com"


def load_cookies() -> list[dict[str, Any]]:
    """Playwright-shaped cookies, or an empty list when nothing is configured."""
    settings = get_settings()
    raw = (settings.linkedin_session_cookie or "").strip()
    path = (settings.linkedin_session_path or "").strip()
    if path:
        file = Path(path)
        if not file.is_file():
            logger.warning("LINKEDIN_SESSION_PATH %s is not a file", path)
        else:
            raw = file.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    return _parse_cookies(raw)


def _parse_cookies(raw: str) -> list[dict[str, Any]]:
    if raw.startswith("[") or raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LinkedIn session cookie JSON did not parse")
            return []
        if isinstance(payload, dict):
            payload = payload.get("cookies") or []
        if not isinstance(payload, list):
            return []
        return [_playwright_cookie(item) for item in payload if isinstance(item, dict)]

    cookies: list[dict[str, Any]] = []
    for part in raw.split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append(_playwright_cookie({"name": name.strip(), "value": value.strip()}))
    return cookies


def _playwright_cookie(item: dict[str, Any]) -> dict[str, Any]:
    name = str(item.get("name") or "").strip()
    value = str(item.get("value") or "").strip()
    domain = str(item.get("domain") or _LINKEDIN_DOMAIN)
    path = str(item.get("path") or "/")
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
    }
    if item.get("secure") is not None:
        cookie["secure"] = bool(item["secure"])
    else:
        cookie["secure"] = True
    if item.get("httpOnly") is not None:
        cookie["httpOnly"] = bool(item["httpOnly"])
    if item.get("sameSite"):
        cookie["sameSite"] = str(item["sameSite"])
    return cookie

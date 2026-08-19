"""Lazy LinkedIn resolution -- only when a human clicks a candidate, never in bulk.

Waterfall, stopping at the first hit (record which step fired in linkedin_source):
  1. a linkedin.com/in URL already in the bio or website field   (free)
  2. the profile README (github.com/{login}/{login})            (free)
  3. the candidate's website homepage                            (free)
  4. Apollo people-match                                          (a credit)

Apollo (step 4) is guarded: it requires an explicit reason (shortlist context)
and every call is logged with the candidate id and reason.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, Protocol

import httpx

from ..log import get_logger

log = get_logger("enrich.linkedin")

LINKEDIN_RE = re.compile(r"https?://(?:[a-z0-9-]+\.)?linkedin\.com/in/[A-Za-z0-9_%-]+", re.IGNORECASE)


def find_linkedin(text: str | None) -> str | None:
    if not text:
        return None
    m = LINKEDIN_RE.search(text)
    return m.group(0).rstrip("/") if m else None


class TextFetcher(Protocol):
    async def get_text(self, url: str) -> str | None: ...


class HttpFetcher:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=5.0,
            follow_redirects=True,
            headers={"User-Agent": "foray/0.1 (+https://app.goforay.io)"},
        )
        self.calls = 0

    async def get_text(self, url: str) -> str | None:
        self.calls += 1
        try:
            resp = await self._client.get(url)
            return resp.text if resp.status_code == 200 else None
        except Exception:  # noqa: BLE001 - a dead link must not crash resolution
            return None

    async def aclose(self) -> None:
        await self._client.aclose()


async def resolve_linkedin(
    node: dict[str, Any],
    *,
    fetcher: TextFetcher,
    allow_apollo: bool = False,
    apollo_reason: str | None = None,
    apollo: Callable[[dict], Awaitable[str | None]] | None = None,
) -> tuple[str | None, str | None]:
    """Return (linkedin_url, source). Stops at the first hit."""
    # 1. already present in bio / website
    for field in ("bio", "websiteUrl"):
        url = find_linkedin(node.get(field))
        if url:
            return url, "bio"

    login = node["login"]

    # 2. profile README (default branch main, then master)
    for branch in ("main", "master"):
        text = await fetcher.get_text(
            f"https://raw.githubusercontent.com/{login}/{login}/{branch}/README.md"
        )
        url = find_linkedin(text)
        if url:
            return url, "readme"

    # 3. website homepage
    website = node.get("websiteUrl")
    if website:
        url = find_linkedin(await fetcher.get_text(website))
        if url:
            return url, "website"

    # 4. Apollo -- guarded, shortlist-only, and logged
    if allow_apollo:
        if not apollo_reason:
            raise ValueError("Apollo requires an explicit reason (shortlist context only)")
        log.info("apollo.call", login=login, reason=apollo_reason)
        url = await apollo(node) if apollo else None
        if url:
            return url, "apollo"

    return None, None


async def resolve_and_store(
    conn,
    login: str,
    *,
    fetcher: TextFetcher | None = None,
    allow_apollo: bool = False,
    apollo_reason: str | None = None,
    apollo: Callable[[dict], Awaitable[str | None]] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Resolve LinkedIn for one candidate. Hits cache permanently; misses cache
    for FORAY_LINKEDIN_RECHECK_DAYS so the 3-fetch waterfall isn't re-run on
    every click. allow_apollo bypasses the negative cache: the paid step exists
    precisely for candidates the free steps already missed."""
    from datetime import datetime, timedelta, timezone

    from ..config import settings

    row = await conn.fetchrow(
        "select c.id, c.login, c.linkedin_url, c.linkedin_source, c.linkedin_checked_at, r.data "
        "from candidates c left join raw_profiles r on r.candidate_id = c.id "
        "where c.login = $1",
        login,
    )
    if row is None:
        return {"login": login, "linkedin_url": None, "linkedin_source": None, "found": False}
    if row["linkedin_url"]:  # cached permanently -- slugs never change
        return {
            "login": login,
            "linkedin_url": row["linkedin_url"],
            "linkedin_source": row["linkedin_source"],
            "found": True,
            "cached": True,
        }
    if not force and not allow_apollo and row["linkedin_checked_at"] is not None:
        age = datetime.now(timezone.utc) - row["linkedin_checked_at"]
        if age < timedelta(days=settings.linkedin_recheck_days):
            return {
                "login": login,
                "linkedin_url": None,
                "linkedin_source": None,
                "found": False,
                "cached": True,
            }

    import json

    node = row["data"] or {"login": login}
    if isinstance(node, str):
        node = json.loads(node)

    owns_fetcher = fetcher is None
    fetcher = fetcher or HttpFetcher()
    try:
        url, source = await resolve_linkedin(
            node,
            fetcher=fetcher,
            allow_apollo=allow_apollo,
            apollo_reason=apollo_reason,
            apollo=apollo,
        )
    finally:
        if owns_fetcher and isinstance(fetcher, HttpFetcher):
            await fetcher.aclose()

    if url:
        await conn.execute(
            "update candidates set linkedin_url = $1, linkedin_source = $2, "
            "linkedin_checked_at = now() where id = $3",
            url,
            source,
            row["id"],
        )
    else:
        await conn.execute(
            "update candidates set linkedin_checked_at = now() where id = $1", row["id"]
        )
    return {"login": login, "linkedin_url": url, "linkedin_source": source, "found": url is not None}

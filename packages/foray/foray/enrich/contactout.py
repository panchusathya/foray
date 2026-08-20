"""ContactOut: personal email addresses, keyed on the LinkedIn URL we already have.

Measured against ten top-scored LinkedIn-only engineers, the same list Apollo was
tested on:

    personal email   7/10 (70%)   vs Apollo 1/10
    work email       5/10 (50%)   vs Apollo 3/10
    any address      9/10 (90%)   vs Apollo 4/10
    latency          128ms - 1.5s, zero errors

That gap is the reason this exists. Corporate addresses are unusable for
recruiting outreach, and Apollo -- a B2B contact database -- holds personal ones
only incidentally.

Two things this does that apollo.py does not, and should:

* goes through ``request_with_retry`` and a ``RateLimiter``, so a 429 backs off
  instead of silently returning nothing;
* keys on ``linkedin_url``. Apollo matches on name + company, which is fuzzy;
  a profile URL is an identity, and it is a column we already populate for every
  LinkedIn-sourced candidate.

Billing is per address *found*, not per lookup, so a miss is free. That is what
makes a bulk action over a shortlist affordable -- but the caller must still
stamp ``contactout_checked_at`` on a miss, or a re-click pays for the hits again.
"""

from __future__ import annotations

import httpx

from ..config import settings
from ..log import get_logger
from ..ratelimit import RateLimiter
from ._http import request_with_retry
from .contacts import FoundEmail, ProviderResult
from .employer import domain_of, is_work_domain

log = get_logger("enrich.contactout")

BASE_URL = "https://api.contactout.com"
ENRICH_PATH = "/v1/linkedin/enrich"
_HTTP_TIMEOUT = 30.0


def _normalise_profile(url: str | None) -> str | None:
    """A canonical linkedin.com/in/<slug> URL.

    Exports carry locale subdomains -- au., vn., lb. -- which are redirects to
    the same profile. The provider requires a URL containing linkedin.com/in/,
    and matching is more reliable against the canonical host.
    """
    raw = (url or "").strip()
    if not raw or "linkedin.com/in/" not in raw.lower():
        return None
    slug = raw.rstrip("/").rsplit("/in/", 1)[-1].split("?")[0].strip()
    return f"https://www.linkedin.com/in/{slug}" if slug else None


def _usable(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return email if "@" in email and " " not in email else None


class ContactOutProvider:
    """EmailProvider over ContactOut's LinkedIn enrichment endpoint."""

    name = "contactout"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._limiter = RateLimiter(settings.contactout_rpm)
        self.calls = 0

    def is_configured(self) -> bool:
        return bool(settings.contactout_api_key)

    async def find_emails(self, node: dict) -> ProviderResult:
        profile = _normalise_profile(node.get("linkedin_url"))
        if not profile:
            # No identity to look up. Returning empty rather than raising lets a
            # bulk caller stamp the miss and move on.
            log.info("contactout.skipped", reason="no_linkedin_url", login=node.get("login"))
            return ProviderResult()

        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT,
            # Not `Authorization` -- ContactOut takes a bare `token` header, and
            # sending a bearer gets a 401 that reads like a bad key.
            headers={"token": settings.contactout_api_key},
        )
        try:
            await self._limiter.wait()
            response = await request_with_retry(
                client,
                "GET",
                f"{BASE_URL}{ENRICH_PATH}",
                log=log,
                on_request=self._count,
                params={"profile": profile},
            )
        finally:
            if owns_client:
                await client.aclose()

        return self._parse(response.json() or {})

    def _count(self) -> None:
        self.calls += 1

    def _parse(self, payload: dict) -> ProviderResult:
        profile = payload.get("profile") or {}
        emails: list[FoundEmail] = []
        seen: set[str] = set()

        # Personal first so that if the same address appears in both lists, the
        # personal classification is the one that reaches UPSERT_EMAIL -- is_work
        # is frozen at first insert, so the first writer decides forever.
        for raw in profile.get("personal_email") or []:
            email = _usable(raw)
            if email and email not in seen:
                seen.add(email)
                emails.append(FoundEmail(email=email, is_work=False))

        for raw in profile.get("work_email") or []:
            email = _usable(raw)
            if email and email not in seen:
                seen.add(email)
                # Trust the provider's own bucket, but sanity-check it: a
                # freemail domain in the work list is a personal address the
                # provider mislabelled, and sending outreach there is fine while
                # recording it as work is not.
                emails.append(FoundEmail(email=email, is_work=is_work_domain(email)))

        company = profile.get("company") or {}
        domain = company.get("domain") or company.get("website")
        employer_domain = None
        if isinstance(domain, str) and domain.strip():
            employer_domain = domain.strip().lower().removeprefix("https://").removeprefix(
                "http://"
            ).removeprefix("www.").split("/")[0] or None
        elif emails:
            work = next((e.email for e in emails if e.is_work), None)
            employer_domain = domain_of(work) if work else None

        return ProviderResult(
            emails=emails,
            linkedin_url=_normalise_profile(profile.get("li_vanity") or profile.get("url")),
            employer_domain=employer_domain,
            api_calls=self.calls,
        )

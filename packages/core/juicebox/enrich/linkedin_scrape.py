"""Fetch a LinkedIn profile in a real browser and parse it into intake fields.

The live path uses Playwright against a logged-in session. Tests never hit
linkedin.com: they call ``parse_profile_html`` against a checked-in fixture,
or install a fake scraper with ``set_scraper``.

LinkedIn ToS / account-ban risk is operator-owned. A miss (auth wall, CAPTCHA,
missing cookies, Playwright not installed) raises ``ScrapeError`` and the
intake thread falls open to a short question batch rather than stalling.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .linkedin_session import load_cookies
from .linkedin_url import canonical_linkedin_url

logger = logging.getLogger(__name__)

_JSON_LD = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_OG_TITLE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_OG_TITLE_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
    re.IGNORECASE,
)
_OG_DESC = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_YEAR_RANGE = re.compile(
    r"(19|20)\d{2}\s*[\u2013\-]\s*((?:19|20)\d{2}|present|now)",
    re.IGNORECASE,
)
_TITLE_AT_COMPANY = re.compile(
    r"^(?P<name>.+?)\s+[-\u2013\u2014]\s+(?P<title>.+?)\s+at\s+(?P<company>.+?)"
    r"(?:\s*[|\u00b7].*)?$",
    re.IGNORECASE,
)
_MGMT_TITLE = re.compile(
    r"\b(manager|director|vp|vice president|head of|chief|cto|ceo|cpo|cfo|coo|"
    r"supervisor|gm)\b",
    re.IGNORECASE,
)

# ~10/min. The lock also serialises Playwright so two threads do not share a
# Chromium with the same session cookie.
_RATE_LOCK = threading.Lock()
_LAST_SCRAPE_AT = 0.0
_MIN_INTERVAL_SECONDS = 6.0
_NAV_TIMEOUT_MS = 30_000

Scraper = Callable[[str], "LinkedInProfile"]
_scraper: Scraper | None = None


class ScrapeError(RuntimeError):
    """The profile could not be read. Safe to show in logs, not to the candidate."""


@dataclass
class LinkedInProfile:
    url: str = ""
    full_name: str = ""
    headline: str = ""
    current_title: str = ""
    current_employer: str = ""
    location: str = ""
    experience_years: float | None = None
    industry: str = ""
    is_ic: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def set_scraper(fn: Scraper | None) -> None:
    """Tests install a fake; production leaves this None."""
    global _scraper
    _scraper = fn


def reset_scraper() -> None:
    set_scraper(None)


def is_ic_title(title: str) -> bool:
    """True when the title does not look like people-management."""
    return not bool(_MGMT_TITLE.search(title or ""))


def seniority_from_years(years: float) -> str:
    """Map a stated year count onto ``ParsedQuery`` seniority buckets."""
    if years < 2:
        return "junior"
    if years < 5:
        return "mid"
    if years < 8:
        return "senior"
    if years < 12:
        return "staff"
    return "principal"


def parse_years_experience(text: str) -> float | None:
    """``8 years``, ``8+ yrs`` — the answer to the IC follow-up, not a title."""
    match = re.search(
        r"\b(\d{1,2})\s*(?:\+\s*)?(?:years?|yrs)\b",
        text or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    return float(match.group(1))


def scrape_profile(url: str) -> LinkedInProfile:
    """Fetch and parse. Tests swap this out with ``set_scraper``."""
    canonical = canonical_linkedin_url(url)
    if not canonical:
        raise ScrapeError("not a linkedin.com/in URL")
    if _scraper is not None:
        return _scraper(canonical)
    html = _playwright_fetch(canonical)
    if looks_like_auth_wall(html):
        raise ScrapeError("linkedin auth wall")
    profile = parse_profile_html(html, url=canonical)
    if not profile.full_name and not profile.current_title:
        raise ScrapeError("profile page had no usable fields")
    return profile


def looks_like_auth_wall(html: str) -> bool:
    lowered = (html or "").lower()
    return (
        "authwall" in lowered
        or "join to view" in lowered
        or "sign in to view" in lowered
    )


def parse_profile_html(html: str, *, url: str = "") -> LinkedInProfile:
    """Pure parse. No network. This is what the HTML-fixture tests call."""
    person = _person_from_json_ld(html)
    og_title = _meta(_OG_TITLE, html) or _meta(_OG_TITLE_REV, html)
    og_desc = _meta(_OG_DESC, html)

    full_name = str(person.get("name") or "")
    headline = str(person.get("jobTitle") or person.get("headline") or "")
    employer = _works_for(person)
    location = _location_of(person)
    title = headline

    if og_title:
        parsed = _TITLE_AT_COMPANY.match(og_title.replace(" | LinkedIn", "").strip())
        if parsed:
            full_name = full_name or parsed.group("name").strip()
            title = title or parsed.group("title").strip()
            employer = employer or parsed.group("company").strip()
        elif not full_name:
            full_name = og_title.split("|")[0].split("-")[0].strip()

    if not headline and og_desc:
        headline = og_desc.split(".")[0].strip()

    years = _years_from_person(person)
    if years is None:
        years = _years_from_text(html)

    title = (title or "").strip()
    # A headline like "Software Engineer at Stripe" is a title+employer, not a
    # title. Split when JSON-LD did not already supply worksFor.
    if title and not employer:
        at = re.search(r"\s+at\s+(.+)$", title, re.IGNORECASE)
        if at:
            employer = at.group(1).strip()
            title = title[: at.start()].strip()

    industry = ""
    works = person.get("worksFor")
    if isinstance(works, dict):
        industry = str(works.get("industry") or works.get("knowsAbout") or "")

    profile = LinkedInProfile(
        url=canonical_linkedin_url(url) or url,
        full_name=full_name.strip(),
        headline=(headline or title).strip(),
        current_title=title,
        current_employer=employer.strip(),
        location=location.strip(),
        experience_years=years,
        industry=industry.strip(),
        is_ic=is_ic_title(title),
        raw={"json_ld": person, "og_title": og_title, "og_description": og_desc},
    )
    return profile


def _meta(pattern: re.Pattern[str], html: str) -> str:
    match = pattern.search(html or "")
    return match.group(1).strip() if match else ""


def _person_from_json_ld(html: str) -> dict[str, Any]:
    for block in _JSON_LD.findall(html or ""):
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        person = _find_person(payload)
        if person:
            return person
    return {}


def _find_person(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, list):
        for item in payload:
            found = _find_person(item)
            if found:
                return found
        return None
    if not isinstance(payload, dict):
        return None
    types = payload.get("@type")
    type_list = types if isinstance(types, list) else [types]
    if any(str(item).lower() == "person" for item in type_list if item):
        return payload
    graph = payload.get("@graph")
    if graph is not None:
        return _find_person(graph)
    return None


def _works_for(person: dict[str, Any]) -> str:
    works = person.get("worksFor")
    if isinstance(works, dict):
        return str(works.get("name") or "")
    if isinstance(works, list) and works:
        first = works[0]
        if isinstance(first, dict):
            return str(first.get("name") or "")
        return str(first)
    return ""


def _location_of(person: dict[str, Any]) -> str:
    address = person.get("address")
    if isinstance(address, str):
        return address
    if isinstance(address, dict):
        parts = [
            str(address.get("addressLocality") or ""),
            str(address.get("addressRegion") or ""),
            str(address.get("addressCountry") or ""),
        ]
        return ", ".join(part for part in parts if part)
    return str(person.get("homeLocation") or person.get("location") or "")


def _years_from_person(person: dict[str, Any]) -> float | None:
    occupation = person.get("hasOccupation") or person.get("alumniOf") or []
    if isinstance(occupation, dict):
        occupation = [occupation]
    if not isinstance(occupation, list):
        return None
    starts: list[int] = []
    ends: list[int] = []
    now_year = datetime.now(UTC).year
    for item in occupation:
        if not isinstance(item, dict):
            continue
        start = _year_of(item.get("startDate") or item.get("foundingDate"))
        end_raw = item.get("endDate")
        end = now_year if not end_raw else _year_of(end_raw)
        if start:
            starts.append(start)
            ends.append(end or now_year)
    if not starts:
        return None
    return float(max(ends) - min(starts))


def _year_of(value: Any) -> int | None:
    text = str(value or "")
    match = re.search(r"(19|20)\d{2}", text)
    return int(match.group(0)) if match else None


def _years_from_text(html: str) -> float | None:
    starts: list[int] = []
    ends: list[int] = []
    now_year = datetime.now(UTC).year
    for match in _YEAR_RANGE.finditer(html or ""):
        start = int(match.group(0)[:4])
        end_token = match.group(2)
        end = now_year if end_token.lower() in {"present", "now"} else int(end_token)
        starts.append(start)
        ends.append(end)
    if not starts:
        return None
    return float(max(0, max(ends) - min(starts)))


def _playwright_fetch(url: str, *, retry: bool = True) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ScrapeError("playwright is not installed") from exc

    cookies = load_cookies()
    _wait_for_rate_limit()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                page.wait_for_timeout(1500)
                html = page.content()
            finally:
                browser.close()
    except PlaywrightTimeout as exc:
        if retry:
            logger.warning("linkedin scrape timed out, retrying once: %s", exc)
            return _playwright_fetch(url, retry=False)
        raise ScrapeError("linkedin navigation timed out") from exc
    except Exception as exc:
        if retry:
            logger.warning("linkedin scrape failed, retrying once: %s", exc)
            return _playwright_fetch(url, retry=False)
        raise ScrapeError(str(exc)) from exc
    return html


def _wait_for_rate_limit() -> None:
    global _LAST_SCRAPE_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL_SECONDS - (now - _LAST_SCRAPE_AT)
        if wait > 0:
            time.sleep(wait)
        _LAST_SCRAPE_AT = time.monotonic()

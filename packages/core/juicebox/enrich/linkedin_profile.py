"""LinkedIn URL → a structured profile, via a fresh-profile API.

The repo's first live profile fetcher. ContactOut was evaluated and is the
wrong tool: its enrich returns name/company/emails but no experience or
education arrays, and the point here is a Candidate with a history. Foray's
``linkedin_map.py`` proved the mapping problem on Bright Data exports; this is
the same discipline against a per-URL vendor (ScrapIn-class), isolated behind
the same one-file adapter pattern as Retell (``intake/providers``) and Linq.

House rules, all enforced here or by the worker handler:

* **Paid calls run only in the worker.** The webhook that receives a LinkedIn
  URL enqueues and returns; a public request path never spends money.
* **A real miss stamps the negative cache** (``profile_ingest_checked_at``) so
  it is never paid for twice; **a vendor failure never does** — a quota wall
  must not permanently mark a person unfetchable (the ContactOut lesson,
  AGENTS.md).
* The vendor's exact wire format lives in ``ScrapinProvider`` alone. Confirm
  during onboarding: auth header name, the profile-response shape below, rate
  limits, and what a private/dead profile returns.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

# linkedin.com/in/<slug>, tolerant of locale subdomains and trailing junk.
# The same canonicalisation ContactOut's adapter does in Foray.
LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9\-_%\.]+)", re.IGNORECASE
)


def find_linkedin(text: str) -> str:
    """The first canonical LinkedIn profile URL in free text, or ''."""
    match = LINKEDIN_RE.search(text or "")
    if not match:
        return ""
    slug = match.group(1).strip("/.").strip()
    if not slug:
        return ""
    return f"https://www.linkedin.com/in/{slug}"


def linkedin_slug(url: str) -> str:
    match = LINKEDIN_RE.search(url or "")
    return match.group(1).strip("/.").lower() if match else ""


class ProfileNotFound(Exception):
    """A real miss: the profile does not exist or is not visible. Cacheable."""


class ProfileProviderUnavailable(RuntimeError):
    """Quota, outage, auth failure: retry later, never stamp the cache."""


class ProfileProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    def fetch(self, linkedin_url: str) -> dict[str, Any]:
        """The vendor's raw payload. Raises the two exceptions above."""
        ...


class ScrapinProvider:
    """ScrapIn-style fresh-profile API. Wire details live here and nowhere
    else; swap the vendor by swapping this class."""

    name = "scrapin"

    def is_configured(self) -> bool:
        return bool(get_settings().scrapin_api_key)

    def fetch(self, linkedin_url: str) -> dict[str, Any]:
        settings = get_settings()
        if not self.is_configured():
            raise ProfileProviderUnavailable("no profile provider API key configured")
        try:
            response = httpx.get(
                f"{settings.scrapin_api_base.rstrip('/')}/enrichment/profile",
                params={"apikey": settings.scrapin_api_key, "linkedInUrl": linkedin_url},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise ProfileProviderUnavailable(f"profile fetch failed: {exc}") from exc
        if response.status_code == 404:
            raise ProfileNotFound(linkedin_url)
        if response.status_code in {401, 402, 403, 429} or response.status_code >= 500:
            raise ProfileProviderUnavailable(
                f"profile provider returned {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProfileProviderUnavailable("profile provider returned non-JSON") from exc
        person = payload.get("person") if isinstance(payload, dict) else None
        if not isinstance(person, dict) or not person:
            # A 200 with no person is the quota-sample trap ContactOut taught
            # us about -- treat as unavailable, never as a cacheable miss.
            raise ProfileProviderUnavailable("profile provider returned an empty person")
        return payload


class ApifyProvider:
    """LinkedIn profiles via an Apify actor run.

    Apify runs a scraper on their infrastructure and hands back dataset items,
    so the wire shape is an actor's output rather than a documented profile
    API. Which actor is configurable (``APIFY_LINKEDIN_ACTOR``) because the
    good ones come and go; what does not change is that the result must leave
    this class in the ScrapIn-style ``person`` envelope, so ``map_profile``
    stays one function and swapping vendors stays a one-class change.

    ``run-sync-get-dataset-items`` blocks until the run finishes and returns
    the items directly, which avoids polling a run id. It also means the
    timeout has to be generous: a browser actor is seconds, not milliseconds.
    """

    name = "apify"

    def is_configured(self) -> bool:
        return bool(get_settings().apify_api_token)

    def fetch(self, linkedin_url: str) -> dict[str, Any]:
        settings = get_settings()
        if not self.is_configured():
            raise ProfileProviderUnavailable("no APIFY_API_TOKEN configured")
        actor = settings.apify_linkedin_actor.replace("/", "~").strip()
        if not actor:
            # Without this the URL is /v2/acts//run-sync-... which returns a
            # 404 that reads exactly like a wrong actor id -- so the one
            # config mistake that produces it is the hardest to diagnose.
            raise ProfileProviderUnavailable("APIFY_LINKEDIN_ACTOR is empty")
        url = (
            f"{settings.apify_base_url.rstrip('/')}/v2/acts/{actor}"
            f"/run-sync-get-dataset-items"
        )
        try:
            response = httpx.post(
                url,
                params={"token": settings.apify_api_token},
                # This actor's input contract: `queries` takes profile URLs
                # (it accepts several; we ask for one so a failure is about
                # one person), and the mode selects what gets scraped and
                # therefore what it costs.
                json={
                    "queries": [linkedin_url],
                    "profileScraperMode": settings.apify_profile_mode,
                },
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise ProfileProviderUnavailable(f"apify run failed: {exc}") from exc

        if response.status_code == 404:
            # The actor id is wrong, not the person. Never cache this as a
            # miss -- that would stamp a candidate unfetchable over a typo.
            raise ProfileProviderUnavailable(f"apify actor {actor!r} not found")
        if response.status_code in {401, 402, 403, 429} or response.status_code >= 500:
            raise ProfileProviderUnavailable(f"apify returned {response.status_code}")

        try:
            items = response.json()
        except ValueError as exc:
            raise ProfileProviderUnavailable("apify returned non-JSON") from exc
        if not isinstance(items, list) or not items:
            # A finished run with no items is the profile being unreachable --
            # private, deleted, or blocked. That is about this person, so it
            # is a real miss and may be cached.
            raise ProfileNotFound(linkedin_url)
        person = items[0]
        if not isinstance(person, dict) or not person:
            raise ProfileProviderUnavailable("apify returned an empty item")
        return {"person": _apify_person(person)}


def _apify_person(item: dict[str, Any]) -> dict[str, Any]:
    """One actor's field names → the envelope ``map_profile`` reads.

    Verified against a live run of ``LpVuK3Zozwuipa5bp``. The differences that
    matter, each of which produced a wrong record before it was translated:

    * the role is ``position``, not ``title`` -- every experience arrived with
      a blank job title, which is most of what a resume is;
    * ``location`` is a nested object, so ``str()`` on it wrote the whole dict
      into the candidate's city field;
    * the bio is ``about``, not ``summary``.

    Everything else passes through untouched, so a richer payload is not
    thrown away and ``map_profile`` keeps the spellings it already knows.
    """
    person = dict(item)

    if not person.get("fullName"):
        full = str(item.get("full_name") or item.get("name") or "").strip()
        if not full:
            full = " ".join(
                str(item.get(k) or "").strip() for k in ("firstName", "lastName")
            ).strip()
        if full:
            person["fullName"] = full

    roles = None
    for key in ("experience", "experiences", "positions", "positions_data"):
        if isinstance(item.get(key), list) and item[key]:
            roles = item[key]
            break
    if roles is not None:
        person["positions"] = [_apify_role(r) for r in roles if isinstance(r, dict)]

    if "schools" not in person and "educationHistory" not in person:
        for key in ("education", "educations", "education_data"):
            if isinstance(item.get(key), list):
                person["schools"] = item[key]
                break

    person["location"] = _apify_location(item.get("location"))

    if not person.get("summary"):
        person["summary"] = str(item.get("about") or "")

    person["skills"] = _apify_skills(item.get("skills") or item.get("topSkills"))
    return person


def _apify_role(role: dict[str, Any]) -> dict[str, Any]:
    """``position`` is the job title. Without this every role is untitled."""
    out = dict(role)
    if not out.get("title"):
        out["title"] = str(role.get("position") or "").strip()
    return out


def _apify_location(value: Any) -> str:
    """The most human of the location spellings, as a plain string.

    ``linkedinText`` is what the profile itself displays ("Seattle,
    Washington, United States"), so it is the one a person would recognise.
    """
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return ""
    parsed = value.get("parsed") if isinstance(value.get("parsed"), dict) else {}
    for candidate in (
        value.get("linkedinText"),
        parsed.get("text"),
        ", ".join(
            x for x in (parsed.get("city"), parsed.get("state"), parsed.get("country")) if x
        ),
    ):
        text = str(candidate or "").strip()
        if text:
            return text
    return ""


def _apify_skills(value: Any) -> list[str]:
    """Skills as names. Gates has none, so the shape is unconfirmed -- accept
    both a list of strings and a list of ``{name: ...}`` rather than let a
    stringified dict reach the search haystack."""
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            text = entry.strip()
        elif isinstance(entry, dict):
            text = str(entry.get("name") or entry.get("skill") or "").strip()
        else:
            text = ""
        if text:
            names.append(text)
    return names


class FakeProfileProvider:
    """Scripted provider for tests: one canned payload per URL."""

    name = "fake"

    def __init__(self, profiles: dict[str, dict] | None = None) -> None:
        self.profiles = dict(profiles or {})
        self.calls: list[str] = []
        self.fail_next: Exception | None = None

    def is_configured(self) -> bool:
        return True

    def fetch(self, linkedin_url: str) -> dict[str, Any]:
        self.calls.append(linkedin_url)
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        try:
            return self.profiles[linkedin_url]
        except KeyError:
            raise ProfileNotFound(linkedin_url) from None


_provider: ProfileProvider | None = None


def get_profile_provider() -> ProfileProvider:
    global _provider
    if _provider is None:
        _provider = (
            ScrapinProvider()
            if get_settings().profile_provider == "scrapin"
            else ApifyProvider()
        )
    return _provider


def set_profile_provider(provider: ProfileProvider | None) -> None:
    global _provider
    _provider = provider


def map_profile(payload: dict[str, Any], *, linkedin_url: str) -> dict[str, Any]:
    """Vendor payload → the ``upsert_candidates`` payload shape.

    The junk discipline is ported from Foray's ``linkedin_map``: dates only as
    given, no title left blank because a company page slipped into a person
    stream, nothing invented. Assumes the ScrapIn-ish ``person`` envelope —
    the one thing to re-verify against a live sandbox response.
    """
    person = payload.get("person") or {}
    positions = _list(person, "positions", "positionHistory", "experience")
    schools = _list(person, "schools", "educationHistory", "education")
    leadership = _leadership(person)

    experiences = []
    for item in positions:
        if not isinstance(item, dict):
            continue
        company = str(item.get("companyName") or item.get("company") or "").strip()
        title = str(item.get("title") or "").strip()
        if not company and not title:
            continue
        start = _date_str(item.get("startEndDate", {}).get("start") if isinstance(item.get("startEndDate"), dict) else item.get("startDate"))
        end_raw = item.get("startEndDate", {}).get("end") if isinstance(item.get("startEndDate"), dict) else item.get("endDate")
        end = _date_str(end_raw)
        experiences.append(
            {
                "company": company,
                "title": title,
                "start_date": start,
                "end_date": end,
                "is_current": not end,
                "description": str(item.get("description") or ""),
            }
        )

    educations = []
    for item in schools:
        if not isinstance(item, dict):
            continue
        school = str(item.get("schoolName") or item.get("school") or "").strip()
        if not school:
            continue
        educations.append(
            {
                "school": school,
                "degree": str(item.get("degreeName") or item.get("degree") or ""),
                "field_of_study": str(item.get("fieldOfStudy") or ""),
                "start_year": _year(item),
                "end_year": _year(item, end=True),
            }
        )

    slug = linkedin_slug(linkedin_url)
    name = str(
        person.get("fullName")
        or " ".join(x for x in (person.get("firstName"), person.get("lastName")) if x)
        or ""
    ).strip()

    return {
        "source": "linkedin_ingest",
        "source_id": slug or linkedin_url,
        "full_name": name,
        "headline": str(person.get("headline") or ""),
        "summary": str(person.get("summary") or ""),
        "location_city": str(person.get("location") or person.get("city") or ""),
        "linkedin_url": f"https://www.linkedin.com/in/{slug}" if slug else linkedin_url,
        "skills": [str(s) for s in person.get("skills") or [] if str(s).strip()],
        "experiences": experiences,
        "educations": educations,
        # Keep volunteer and organization entries beside the raw payload. The
        # CRM deliberately has no child table for them, but the generated
        # resume reads this provenance artifact and can render the section.
        "raw": {"profile_provider": payload, "leadership": leadership},
    }


def _list(person: dict, *keys: str) -> list:
    for key in keys:
        value = person.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict) and isinstance(value.get("items"), list):
            return value["items"]
    return []


def _leadership(person: dict) -> list[dict]:
    """Normalize the common provider names for LinkedIn volunteer roles.

    These are resume facts, not professional employment: keeping them out of
    ``experiences`` prevents a campus organization from distorting tenure while
    preserving it in the profile artifact for the resume renderer.
    """
    entries = _list(
        person,
        "volunteerExperience",
        "volunteer_experience",
        "volunteer",
        "organizations",
        "organizationExperience",
    )
    out: list[dict] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        company = str(
            item.get("organizationName") or item.get("companyName") or item.get("company") or ""
        ).strip()
        title = str(item.get("role") or item.get("title") or item.get("position") or "").strip()
        if not company and not title:
            continue
        date_range = item.get("startEndDate") if isinstance(item.get("startEndDate"), dict) else {}
        start = _date_str(date_range.get("start") or item.get("startDate"))
        end = _date_str(date_range.get("end") or item.get("endDate")) or "present"
        description = str(item.get("description") or item.get("details") or "").strip()
        out.append(
            {
                "company": company,
                "title": title,
                "start": start,
                "end": end,
                "location": str(item.get("location") or "").strip(),
                "bullets": [description] if description else [],
            }
        )
    return out


def _date_str(value: Any) -> str:
    """YYYY or YYYY-MM from the shapes these vendors use; '' when absent."""
    if isinstance(value, dict):
        year = value.get("year")
        month = value.get("month")
        if not year:
            return ""
        if month:
            return f"{int(year):04d}-{int(month):02d}"
        return f"{int(year):04d}"
    if isinstance(value, str):
        return value.strip()[:7]
    return ""


def _year(item: dict, *, end: bool = False) -> int | None:
    span = item.get("startEndDate")
    side = (span or {}).get("end" if end else "start") if isinstance(span, dict) else None
    if isinstance(side, dict) and side.get("year"):
        try:
            return int(side["year"])
        except (TypeError, ValueError):
            return None
    return None

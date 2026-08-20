"""Pure mapping from a Bright Data LinkedIn record to Foray's shapes.

No database, no network — everything here is unit-testable against a sample
of the real export. Field quirks this encodes (measured on the actual file):

  * ``position`` is unreliable: the most common values are ``--``,
    ``set at linkedin`` and bare company names. ``experience[0].title`` is the
    primary title source, ``position`` a filtered fallback.
  * ``experience`` entries can be company-grouped, with the real roles nested
    in ``positions`` and the parent's ``title`` set to the company name.
  * Dates arrive as ``"Jul 2017"`` / ``"2017"`` / ``"Present"``.
  * ~2% of "San Francisco" rows are San Franciscos in MX/AR/CR — the
    ``country_code`` field disambiguates.
  * There is no skills[] field and effectively no emails in the export.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# position values that are noise, not titles (lowercased comparison).
_JUNK_TITLES = {"", "--", "-", "set at linkedin", "retired", "n/a", "none"}

# current_company names that are placeholders, not employers (lowercased
# comparison). Deliberately minimal: "stealth" / "stealth startup",
# "self-employed" and "freelance" are NOT junk -- they carry real signal (a
# founding-engineer search wants exactly the stealth people), whereas
# LinkedIn's "Confidential Company" and punctuation filler say nothing and
# would pollute candidate_signals.employer, the weight-A tsvector and the
# embedding blob.
_JUNK_EMPLOYERS = {"confidential", "confidential company", "n/a", "none", "-", "--", "."}

# Two token classes mark a "person" row as a company page, validated against
# the full prod corpus (196,952 linkedin names; the tightened rules kept 174
# true company pages and cleared ~100 false positives the first cut had):
#   * LEGAL suffixes match ANYWHERE, case-insensitive, whole-word: no real
#     person's name contains "LLC" or "GmbH" in any position.
#     - "co." keeps its dot -- bare "co" is a real surname ("Jose Co"),
#       while "Co." mid-name is unambiguously corporate.
#     - "s.a" only matches at the END of the name: corporate "S.A." is a
#       trailing suffix ("Banco Santander S.A."), while the same letters at
#       the front are a person's initials ("S.A. Khan").
#     - "ltd"/"limited" catch sole traders like "Mary-Jane O'Brien Ltd" -- a
#       person trading as a company IS a company page here; accepted.
#   * DESCRIPTIVE org words ("capital", "group", "solutions", ...) match only
#     as the LAST word: companies end with them ("Madison Group", "Finnoble
#     Solutions"), while mid-name occurrences are real people ("CAPITAL R.
#     MAAT", "Sylvia Jones ... Staffing ... Consulting" -- both live in the
#     export). A trailing scraper artifact ("PulseChart Solutions undefined")
#     and trailing punctuation are tolerated after the word.
# Two heuristics were REMOVED after the prod validation caught them deleting
# real people, so do not reintroduce them:
#   * trailing parenthesised initialism: "(IU)" looked org-shaped, but the
#     corpus is full of credentialed people -- "(MBA)", "(CISSP)", "(ASCP)",
#     "(LION)", "Anand Vijay(AV)". Every true org it caught also carries a
#     legal suffix.
#   * ampersand with 3+ words: caught couples' profiles ("John & Charlotte
#     Schmiedel"), which are person pages.
# The cost is a handful of suffix-less orgs slipping through ("WestEnd
# Capital Management", "MGA Nurse Staffing San Francisco"); they are
# linkedin-only, unscored rows the archetype gate and ranking already bury,
# while a false positive here DELETES a real candidate in the purge script.
# Dotted tokens escape their dots; the lookarounds below replace \b so a
# trailing dot still terminates a match (\b after "\." would require a
# following word char and miss end-of-name).
_COMPANY_LEGAL_SUFFIXES = (
    "inc", "llc", r"l\.l\.c", "ltd", "limited", "corp", "corporation", "plc",
    "gmbh", "llp", r"s\.a\.?(?=\s*$)", r"s\.r\.l", "pvt", "pty", r"co\.",
)

_COMPANY_ORG_LAST_WORDS = (
    "holdings", "ventures", "capital", "partners", "staffing", "recruiting",
    "agency", "consultancy", "logistics", "solutions", "technologies", "labs",
    "group",
)

_COMPANY_LEGAL_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(_COMPANY_LEGAL_SUFFIXES) + r")(?!\w)", re.IGNORECASE
)

_COMPANY_ORG_LAST_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(_COMPANY_ORG_LAST_WORDS)
    + r")(?!\w)(?:\s+undefined)?[\s.)]*$",
    re.IGNORECASE,
)


def looks_like_company(name: str) -> bool:
    """True when a Bright Data "person" name is actually a company page.

    Conservative by construction -- the purge script deletes on this verdict,
    so every rule is a pattern real people's names essentially never carry
    (see the analysis on the constants above).
    """
    if not name or not name.strip():
        return False
    return bool(
        _COMPANY_LEGAL_RE.search(name) or _COMPANY_ORG_LAST_RE.search(name)
    )

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SLUG_RE = re.compile(r"/in/([^/?#]+)")

MAX_EXPERIENCE_YEARS = 45.0


def slug_of(record: dict) -> str | None:
    """The stable LinkedIn identity — the profile slug."""
    for key in ("linkedin_id", "id"):
        value = (record.get(key) or "").strip().lower()
        if value:
            return value
    for key in ("url", "input_url"):
        match = _SLUG_RE.search(record.get(key) or "")
        if match:
            return match.group(1).strip().lower()
    return None


def parse_month(value: str | None) -> datetime | None:
    """``"Jul 2017"`` / ``"2017"`` -> a UTC datetime; Present/None -> None."""
    text = (value or "").strip().lower()
    if not text or text == "present":
        return None
    parts = text.split()
    try:
        if len(parts) == 2 and parts[0][:3] in _MONTHS:
            return datetime(int(parts[1]), _MONTHS[parts[0][:3]], 1, tzinfo=UTC)
        if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) == 4:
            return datetime(int(parts[0]), 1, 1, tzinfo=UTC)
    except ValueError:
        return None
    return None


@dataclass
class Role:
    title: str
    company: str
    start: datetime | None
    end: datetime | None
    current: bool
    description: str = ""


def flatten_experience(record: dict) -> list[Role]:
    """Normalise experience, unwrapping company-grouped ``positions``."""
    roles: list[Role] = []
    for entry in record.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        company = (entry.get("company") or entry.get("subtitle") or "").strip()
        positions = entry.get("positions")
        if positions and isinstance(positions, list):
            for position in positions:
                if not isinstance(position, dict):
                    continue
                end_raw = position.get("end_date")
                roles.append(
                    Role(
                        title=(position.get("title") or "").strip(),
                        company=(position.get("subtitle") or company).strip(),
                        start=parse_month(position.get("start_date")),
                        end=parse_month(end_raw),
                        current=(end_raw or "").strip().lower() in ("", "present"),
                        description=(position.get("description") or "").strip(),
                    )
                )
            continue
        title = (entry.get("title") or "").strip()
        # A company-grouped parent repeats the company as its title.
        if title.lower() == company.lower():
            title = ""
        end_raw = entry.get("end_date")
        roles.append(
            Role(
                title=title,
                company=company,
                start=parse_month(entry.get("start_date")),
                end=parse_month(end_raw),
                current=(end_raw or "").strip().lower() in ("", "present"),
                description=(entry.get("description") or "").strip(),
            )
        )
    return roles


def best_title(record: dict, roles: list[Role] | None = None) -> str | None:
    roles = flatten_experience(record) if roles is None else roles
    for role in roles:
        if role.title and role.title.lower() not in _JUNK_TITLES:
            return role.title[:200]
    position = (record.get("position") or "").strip()
    company = (record.get("current_company_name") or "").strip()
    if (
        position
        and position.lower() not in _JUNK_TITLES
        and position.lower() != company.lower()
    ):
        return position[:200]
    return None


def employer_of(record: dict) -> str | None:
    current = record.get("current_company") or {}
    name = (current.get("name") if isinstance(current, dict) else "") or ""
    name = name.strip() or (record.get("current_company_name") or "").strip()
    # Placeholders return None so employer_source stays None and the junk
    # never reaches candidate_signals, the tsvector or the embedding blob.
    if name.lower() in _JUNK_EMPLOYERS:
        return None
    return name[:200] or None


def current_role_start(roles: list[Role]) -> datetime | None:
    dated = [role.start for role in roles if role.current and role.start]
    return min(dated) if dated else None


def experience_years(roles: list[Role], *, asof: datetime) -> tuple[float | None, str]:
    """(years, confidence) from the earliest dated role. Real dates beat any
    proxy, so >=2 dated roles is high confidence."""
    starts = [role.start for role in roles if role.start]
    if not starts:
        return None, "low"
    years = (asof - min(starts)).days / 365.25
    years = max(0.0, min(years, MAX_EXPERIENCE_YEARS))
    confidence = "high" if len(starts) >= 2 else "medium"
    return round(years, 1), confidence


def tenure_months(roles: list[Role], *, asof: datetime) -> int | None:
    start = current_role_start(roles)
    if start is None:
        return None
    return max(0, int((asof - start).days / 30.44))


def is_target_geo(record: dict) -> bool:
    """Genuinely US Bay Area — the export carries San Franciscos in MX/AR/CR."""
    if (record.get("country_code") or "").upper() != "US":
        return False
    return bool((record.get("city") or "").strip())


def pseudo_repos(record: dict, roles: list[Role] | None = None) -> list[dict]:
    """LinkedIn prose reshaped for ``detect_markers`` — technology terms appear
    verbatim in headlines, about text and role descriptions."""
    roles = flatten_experience(record) if roles is None else roles
    repos: list[dict] = []
    position = (record.get("position") or "").strip()
    if position and position.lower() not in _JUNK_TITLES:
        repos.append({"name": "", "description": position, "topics": []})
    about = (record.get("about") or "").strip()
    if about:
        repos.append({"name": "", "description": about[:2000], "topics": []})
    for role in roles[:6]:
        text = " ".join(part for part in (role.title, role.description[:500]) if part)
        if text:
            repos.append({"name": "", "description": text, "topics": []})
    for certification in record.get("certifications") or []:
        if isinstance(certification, dict) and certification.get("title"):
            repos.append({"name": "", "description": certification["title"], "topics": []})
    return repos


def business_emails(record: dict) -> list[str]:
    """The rare address hiding in free text (~0.2% of the export)."""
    haystack = " ".join(
        str(record.get(key) or "") for key in ("about", "bio_links", "educations_details")
    )
    return sorted({match.group(0).lower() for match in _EMAIL_RE.finditer(haystack)})


@dataclass
class ImportRow:
    slug: str
    login: str
    name: str
    location_raw: str | None
    linkedin_url: str
    title: str | None
    employer: str | None
    emails: list[str] = field(default_factory=list)


def to_import_row(record: dict) -> ImportRow | None:
    """None = unusable record (no slug, no name, or a company page that the
    export mixed into the person stream); the stream skips it."""
    slug = slug_of(record)
    name = (record.get("name") or "").strip()
    if not slug or not name:
        return None
    if looks_like_company(name):
        return None
    roles = flatten_experience(record)
    url = (record.get("url") or "").split("?")[0] or f"https://www.linkedin.com/in/{slug}"
    if url.startswith("http://"):  # a fraction of the export is plain http
        url = "https://" + url[len("http://"):]
    return ImportRow(
        slug=slug,
        login=f"li:{slug}",
        name=name[:200],
        location_raw=(record.get("city") or "").strip() or None,
        linkedin_url=url,
        title=best_title(record, roles),
        employer=employer_of(record),
        emails=business_emails(record),
    )

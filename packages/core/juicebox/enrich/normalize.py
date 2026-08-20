"""Turn a messy inbound profile into a canonical Candidate row.

Everything downstream (search, scoring, outreach) reads the normalised fields,
so this module is the single place raw source quirks get ironed out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from . import taxonomy as tx

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")
_MONTH_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?")


@dataclass
class NormalizedExperience:
    company: str = ""
    company_domain: str = ""
    company_industry: str = ""
    company_size: str = ""
    company_stage: str = ""
    title: str = ""
    title_normalized: str = ""
    seniority: str = "unknown"
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False
    months: int = 0
    location: str = ""
    description: str = ""


@dataclass
class NormalizedEducation:
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_year: int | None = None
    end_year: int | None = None


@dataclass
class NormalizedCandidate:
    source: str
    source_id: str
    full_name: str
    first_name: str = ""
    last_name: str = ""
    headline: str = ""
    summary: str = ""
    location_city: str = ""
    location_region: str = ""
    location_country: str = ""
    open_to_remote: bool = False
    linkedin_url: str = ""
    github_url: str = ""
    website_url: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    skills_raw: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    current_title: str = ""
    current_title_normalized: str = ""
    current_company: str = ""
    current_company_domain: str = ""
    seniority: str = "unknown"
    years_experience: float = 0.0
    doc_text: str = ""
    experiences: list[NormalizedExperience] = field(default_factory=list)
    educations: list[NormalizedEducation] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# small parsers
# --------------------------------------------------------------------------


def split_name(full_name: str) -> tuple[str, str]:
    parts = [p for p in (full_name or "").strip().split() if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def clean_emails(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        email = (value or "").strip().lower()
        if _EMAIL_RE.match(email) and email not in out:
            out.append(email)
    return out


# --- placeholder addresses -------------------------------------------------
#
# Juicebox trusts whatever ends up in Candidate.emails: primary_email is
# emails[0] and send_step sends there. That trust is only safe if nothing
# upstream can put filler in the list -- and something did. ContactOut, once its
# trial quota was spent, answered HTTP 200 with a complete sample profile
# carrying email1@example.com and email2@gmail.com, and 30 imported candidates
# ended up with that pair. email2@gmail.com sorted first, so it would have been
# the send address.
#
# Foray has its own copy of this rule where provider results are written. This
# one exists because addresses reach Juicebox by more routes than that --
# import, manual add, a pasted CSV -- and the cost is asymmetric: a rejected
# real address is a mistake a human can see, and a stored fake one is a message
# to a stranger.
#
# Deliberately narrower than the obvious rule. Matching every filler word would
# condemn mail@danijar.com and email@jamie.sh, which are real people's real
# addresses on their own domains; a first draft of the Foray rule did exactly
# that, and a dry run caught it holding fifteen genuine addresses.

# RFC 2606 / RFC 6761 reserve these precisely so documentation can use them
# without reaching a mailbox.
RESERVED_DOMAINS = frozenset(
    {"example.com", "example.net", "example.org", "invalid", "localhost", "test"}
)
RESERVED_SUFFIXES = (".example", ".invalid", ".localhost", ".test")

# Filler on any domain: nobody's real address.
_ALWAYS_FILLER = re.compile(
    r"^(?:sample|example|your[-_.]?e?-?mail|first[-_.]?last|full[-_.]?name|"
    r"no-?reply|do-?not-?reply|email[-_.]?address)$",
    re.IGNORECASE,
)

# A numbered filler local part -- what a generated sample looks like, and what a
# person essentially never picks.
_NUMBERED_FILLER = re.compile(
    r"^(?:e?-?mail|test|sample|user|username|name|foo|bar|baz)[-_.]?\d{1,3}$", re.IGNORECASE
)

# The same words unnumbered. Real on a personal domain, filler on freemail.
_BARE_FILLER = re.compile(r"^(?:e?-?mail|test|sample|user|username|name|foo|bar|baz)$", re.IGNORECASE)

# Enough of the common ones to make the bare-filler rule useful. Not a complete
# list and does not need to be: missing one only means a filler address gets
# through to a human's eyes, which is where it was going anyway.
FREEMAIL_DOMAINS = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "hotmail.co.uk",
        "hotmail.com",
        "icloud.com",
        "live.com",
        "mac.com",
        "mail.com",
        "me.com",
        "outlook.com",
        "protonmail.com",
        "yahoo.co.uk",
        "yahoo.com",
        "ymail.com",
    }
)


def is_placeholder(email: str) -> bool:
    """Whether this address is documentation filler rather than a person."""
    local, _, domain = (email or "").strip().lower().rpartition("@")
    if not local or not domain:
        return True
    if domain in RESERVED_DOMAINS or domain.endswith(RESERVED_SUFFIXES):
        return True
    if _ALWAYS_FILLER.match(local) or _NUMBERED_FILLER.match(local):
        return True
    if _BARE_FILLER.match(local):
        return domain in FREEMAIL_DOMAINS
    return False


def strip_placeholders(emails: list[str] | None) -> tuple[list[str], list[str]]:
    """Split a candidate's addresses into keep and drop, order preserved."""
    keep: list[str] = []
    drop: list[str] = []
    for email in emails or []:
        (drop if is_placeholder(email) else keep).append(email)
    return keep, drop


def clean_phones(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        digits = re.sub(r"[^\d+]", "", value or "")
        if len(re.sub(r"\D", "", digits)) >= 7 and digits not in out:
            out.append(digits)
    return out


def parse_year_month(value: str) -> tuple[int, int] | None:
    """Accepts ``2021``, ``2021-04``, ``2021/4``, ``Apr 2021``."""
    if not value:
        return None
    text = str(value).strip().replace("/", "-")
    match = _MONTH_RE.match(text)
    if match:
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        if 1 <= month <= 12 and 1900 <= year <= 2100:
            return year, month
    for fmt in ("%b %Y", "%B %Y", "%m-%Y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.year, parsed.month
        except ValueError:
            continue
    return None


def format_year_month(value: str) -> str:
    parsed = parse_year_month(value)
    return f"{parsed[0]:04d}-{parsed[1]:02d}" if parsed else ""


def months_between(start: str, end: str, *, today: date | None = None) -> int:
    start_ym = parse_year_month(start)
    if not start_ym:
        return 0
    end_ym = parse_year_month(end)
    if not end_ym:
        ref = today or datetime.now(UTC).date()
        end_ym = (ref.year, ref.month)
    months = (end_ym[0] - start_ym[0]) * 12 + (end_ym[1] - start_ym[1])
    return max(months, 0)


def split_location(location: str) -> tuple[str, str, str]:
    """``"San Francisco, CA, United States"`` -> (city, region, country)."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if not parts:
        return "", "", ""
    if len(parts) == 1:
        return parts[0], "", ""
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return parts[0], parts[1], parts[-1]


# --------------------------------------------------------------------------
# experience roll-up
# --------------------------------------------------------------------------


def merge_intervals(intervals: list[tuple[int, int]]) -> int:
    """Total months covered by possibly-overlapping (start, end) month indexes."""
    if not intervals:
        return 0
    ordered = sorted(intervals)
    total = 0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return max(total, 0)


def compute_years_experience(
    experiences: list[NormalizedExperience], *, today: date | None = None
) -> float:
    """Union of employment spans, so concurrent roles aren't double counted."""
    ref = today or datetime.now(UTC).date()
    intervals: list[tuple[int, int]] = []
    for exp in experiences:
        start = parse_year_month(exp.start_date)
        if not start:
            continue
        end = parse_year_month(exp.end_date) or (ref.year, ref.month)
        start_idx = start[0] * 12 + start[1]
        end_idx = end[0] * 12 + end[1]
        if end_idx > start_idx:
            intervals.append((start_idx, end_idx))
    return round(merge_intervals(intervals) / 12.0, 1)


# --------------------------------------------------------------------------
# main entry point
# --------------------------------------------------------------------------


def normalize_experience(payload: dict[str, Any]) -> NormalizedExperience:
    title = (payload.get("title") or "").strip()
    company = (payload.get("company") or "").strip()
    start = format_year_month(payload.get("start_date") or payload.get("starts_at") or "")
    end = format_year_month(payload.get("end_date") or payload.get("ends_at") or "")
    is_current = bool(payload.get("is_current")) or (bool(start) and not end)
    return NormalizedExperience(
        company=company,
        company_domain=(payload.get("company_domain") or "").strip().lower(),
        company_industry=tx.normalize_industry(payload.get("company_industry") or ""),
        company_size=tx.normalize_company_size(payload.get("company_size") or ""),
        company_stage=tx.normalize_company_stage(payload.get("company_stage") or ""),
        title=title,
        title_normalized=tx.normalize_title(title),
        seniority=tx.infer_seniority_from_title(title),
        start_date=start,
        end_date=end,
        is_current=is_current,
        months=months_between(start, end),
        location=(payload.get("location") or "").strip(),
        description=(payload.get("description") or "").strip(),
    )


def normalize_education(payload: dict[str, Any]) -> NormalizedEducation:
    def _year(value: Any) -> int | None:
        try:
            year = int(str(value)[:4])
        except (TypeError, ValueError):
            return None
        return year if 1900 <= year <= 2100 else None

    return NormalizedEducation(
        school=(payload.get("school") or payload.get("institution") or "").strip(),
        degree=(payload.get("degree") or "").strip(),
        field_of_study=(payload.get("field_of_study") or payload.get("field") or "").strip(),
        start_year=_year(payload.get("start_year")),
        end_year=_year(payload.get("end_year")),
    )


def build_doc_text(candidate: NormalizedCandidate) -> str:
    """The blob fed to BM25 and the embedding model."""
    chunks: list[str] = [
        candidate.full_name,
        candidate.headline,
        candidate.current_title,
        candidate.current_title_normalized,
        candidate.current_company,
        candidate.seniority,
        candidate.summary,
        " ".join(candidate.skills),
        " ".join(candidate.industries),
        " ".join(
            part
            for part in (
                candidate.location_city,
                candidate.location_region,
                candidate.location_country,
            )
            if part
        ),
    ]
    for exp in candidate.experiences:
        chunks.append(
            " ".join(
                part
                for part in (
                    exp.title,
                    exp.title_normalized,
                    exp.company,
                    exp.company_industry,
                    exp.company_stage,
                    exp.location,
                    exp.description,
                )
                if part
            )
        )
    for edu in candidate.educations:
        chunks.append(" ".join(p for p in (edu.school, edu.degree, edu.field_of_study) if p))
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def normalize_candidate(payload: dict[str, Any], *, today: date | None = None) -> NormalizedCandidate:
    """Normalise one raw profile dict into the canonical shape."""
    full_name = (payload.get("full_name") or payload.get("name") or "").strip()
    if not full_name:
        first = (payload.get("first_name") or "").strip()
        last = (payload.get("last_name") or "").strip()
        full_name = f"{first} {last}".strip()
    if not full_name:
        raise ValueError("candidate requires a name")

    first_name, last_name = split_name(full_name)
    first_name = (payload.get("first_name") or first_name).strip()
    last_name = (payload.get("last_name") or last_name).strip()

    experiences = [normalize_experience(e) for e in payload.get("experiences") or []]
    experiences.sort(key=lambda e: (e.is_current, e.start_date or ""), reverse=True)
    educations = [normalize_education(e) for e in payload.get("educations") or []]

    city = (payload.get("location_city") or "").strip()
    region = (payload.get("location_region") or "").strip()
    country = (payload.get("location_country") or "").strip()
    if not any((city, region, country)):
        city, region, country = split_location(payload.get("location") or "")

    current = next((e for e in experiences if e.is_current), experiences[0] if experiences else None)
    current_title = (payload.get("current_title") or (current.title if current else "")).strip()
    current_company = (
        payload.get("current_company") or (current.company if current else "")
    ).strip()

    headline = (payload.get("headline") or "").strip()
    if not headline and current_title:
        headline = f"{current_title} at {current_company}" if current_company else current_title

    summary = (payload.get("summary") or payload.get("bio") or "").strip()

    skills_raw = [s for s in (payload.get("skills") or []) if isinstance(s, str)]
    skills = tx.normalize_skills(skills_raw)
    prose = " ".join(
        filter(
            None,
            [headline, summary] + [e.description for e in experiences] + [e.title for e in experiences],
        )
    )
    for implicit in tx.extract_skills_from_text(prose):
        if implicit not in skills:
            skills.append(implicit)

    industries = [
        i for i in {tx.normalize_industry(x) for x in (payload.get("industries") or [])} if i
    ]
    for exp in experiences:
        if exp.company_industry and exp.company_industry not in industries:
            industries.append(exp.company_industry)

    seniority = tx.normalize_seniority(payload.get("seniority") or "")
    if seniority == "unknown":
        seniority = tx.infer_seniority_from_title(current_title)

    years = payload.get("years_experience")
    try:
        years_value = float(years) if years is not None else compute_years_experience(
            experiences, today=today
        )
    except (TypeError, ValueError):
        years_value = compute_years_experience(experiences, today=today)

    source = (payload.get("source") or "manual").strip() or "manual"
    source_id = str(
        payload.get("source_id")
        or payload.get("id")
        or payload.get("linkedin_url")
        or (clean_emails(payload.get("emails"))[:1] or [""])[0]
        or full_name.lower().replace(" ", "-")
    )

    candidate = NormalizedCandidate(
        source=source,
        source_id=source_id,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=headline,
        summary=summary,
        location_city=city,
        location_region=region,
        location_country=country,
        open_to_remote=bool(payload.get("open_to_remote")),
        linkedin_url=(payload.get("linkedin_url") or "").strip(),
        github_url=(payload.get("github_url") or "").strip(),
        website_url=(payload.get("website_url") or "").strip(),
        emails=clean_emails(payload.get("emails")),
        phones=clean_phones(payload.get("phones")),
        skills=skills,
        skills_raw=skills_raw,
        industries=industries,
        current_title=current_title,
        current_title_normalized=tx.normalize_title(current_title),
        current_company=current_company,
        current_company_domain=(
            payload.get("current_company_domain") or (current.company_domain if current else "")
        ).strip(),
        seniority=seniority,
        years_experience=years_value,
        experiences=experiences,
        educations=educations,
        raw=payload.get("raw") or {},
    )
    candidate.doc_text = build_doc_text(candidate)
    return candidate

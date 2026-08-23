"""Natural language -> structured query.

Two implementations behind one call:

  * ``llm_parse``       — Claude with a strict JSON schema (best quality)
  * ``heuristic_parse`` — deterministic regex + taxonomy pass (always available)

``parse_query`` prefers the model and silently falls back, so search never hard
fails because of an API problem.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..enrich import taxonomy as tx
from ..llm.client import LLMUnavailable, get_llm
from ..llm.embeddings import STOPWORDS
from ..llm.prompts import QUERY_PARSER_SCHEMA, QUERY_PARSER_SYSTEM, query_parser_prompt

logger = logging.getLogger(__name__)


@dataclass
class ParsedQuery:
    job_titles: list[str] = field(default_factory=list)
    title_exclusions: list[str] = field(default_factory=list)
    must_have_skills: list[str] = field(default_factory=list)
    nice_to_have_skills: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    min_years_experience: float | None = None
    max_years_experience: float | None = None
    locations: list[str] = field(default_factory=list)
    remote_ok: bool | None = None
    # Stated pay, annualised. A range keeps both ends because the ceiling is
    # what a candidate's floor has to clear -- see jobmatch.preference. A
    # posting stating only a floor has no ceiling to test, which is why these
    # are two fields rather than one number.
    min_compensation: float | None = None
    max_compensation: float | None = None
    compensation_currency: str = ""
    industries: list[str] = field(default_factory=list)
    company_stages: list[str] = field(default_factory=list)
    company_sizes: list[str] = field(default_factory=list)
    target_companies: list[str] = field(default_factory=list)
    excluded_companies: list[str] = field(default_factory=list)
    schools: list[str] = field(default_factory=list)
    degrees: list[str] = field(default_factory=list)
    fields_of_study: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    negative_keywords: list[str] = field(default_factory=list)
    freeform_criteria: list[str] = field(default_factory=list)
    search_text: str = ""

    # -- helpers ---------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ParsedQuery:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (payload or {}).items() if k in known and v is not None})

    def normalized(self) -> ParsedQuery:
        """Push every free-text field through the taxonomy."""
        return ParsedQuery(
            job_titles=_dedupe(tx.normalize_title(t) for t in self.job_titles),
            title_exclusions=_dedupe(tx.normalize_title(t) for t in self.title_exclusions),
            must_have_skills=_dedupe(tx.normalize_skill(s) for s in self.must_have_skills),
            nice_to_have_skills=_dedupe(tx.normalize_skill(s) for s in self.nice_to_have_skills),
            seniority=_dedupe(tx.normalize_seniority(s) for s in self.seniority),
            min_years_experience=self.min_years_experience,
            max_years_experience=self.max_years_experience,
            locations=_dedupe(tx.normalize_location(loc) for loc in self.locations),
            remote_ok=self.remote_ok,
            min_compensation=self.min_compensation,
            max_compensation=self.max_compensation,
            compensation_currency=(self.compensation_currency or "").strip().upper(),
            industries=_dedupe(tx.normalize_industry(i) for i in self.industries),
            company_stages=_dedupe(tx.normalize_company_stage(s) for s in self.company_stages),
            company_sizes=_dedupe(tx.normalize_company_size(s) for s in self.company_sizes),
            target_companies=_dedupe(tx.normalize_company(c) for c in self.target_companies),
            excluded_companies=_dedupe(tx.normalize_company(c) for c in self.excluded_companies),
            schools=_dedupe(tx.slugify_text(s) for s in self.schools),
            degrees=_dedupe(tx.slugify_text(d) for d in self.degrees),
            fields_of_study=_dedupe(tx.slugify_text(f) for f in self.fields_of_study),
            keywords=_dedupe(tx.slugify_text(k) for k in self.keywords),
            negative_keywords=_dedupe(tx.slugify_text(k) for k in self.negative_keywords),
            freeform_criteria=[c.strip() for c in self.freeform_criteria if c and c.strip()],
            search_text=self.search_text.strip(),
        )

    def merge_overrides(self, overrides: dict[str, Any] | None) -> ParsedQuery:
        """UI facet selections win over anything inferred from the sentence."""
        if not overrides:
            return self
        merged = ParsedQuery.from_dict(self.to_dict())
        for key, value in overrides.items():
            if value is None or not hasattr(merged, key):
                continue
            if isinstance(value, list) and not value:
                continue
            setattr(merged, key, value)
        return merged.normalized()

    def retrieval_text(self) -> str:
        """The text used for BM25 + embedding retrieval."""
        parts = [
            self.search_text,
            " ".join(self.job_titles),
            " ".join(self.must_have_skills),
            " ".join(self.nice_to_have_skills),
            " ".join(self.seniority),
            " ".join(self.locations),
            " ".join(self.industries),
            " ".join(self.target_companies),
            " ".join(self.keywords),
            " ".join(self.freeform_criteria),
        ]
        return " ".join(p for p in parts if p).strip()

    def is_empty(self) -> bool:
        return not any(
            [
                self.job_titles,
                self.must_have_skills,
                self.nice_to_have_skills,
                self.seniority,
                self.locations,
                self.industries,
                self.target_companies,
                self.keywords,
                self.freeform_criteria,
                self.min_years_experience,
                self.min_compensation,
                self.max_compensation,
            ]
        )


def _dedupe(values) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        cleaned = (value or "").strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


# --------------------------------------------------------------------------
# stated compensation
# --------------------------------------------------------------------------

# A money amount only counts inside a clause that is about pay. Same rule as
# ``intake.extract._heuristic_comp``, for the same reason: "we raised $200
# million" and "$5B in revenue" appear in job descriptions constantly.
_COMP_CUE = re.compile(
    r"\b(base|salary|salaries|comp|compensation|package|remuneration|ote"
    r"|all[- ]in|pays?|paying|paid|earn(?:ing)?s?)\b",
    re.I,
)
# Clauses about the company's money, not the role's. A funding round lands in
# the plausible-salary band often enough that the band alone is not a filter.
_COMP_NOISE = re.compile(
    r"\b(raised|raising|funding|valuation|valued|revenue|arr|acquisition"
    r"|market cap|assets under management|aum)\b",
    re.I,
)
# "401k match" is in half of all job descriptions and parses as $401,000.
_RETIREMENT_PLAN = re.compile(r"\b40[13]\s*\(?\s*[kb]\s*\)?", re.I)
_COMP_CLAUSE = re.compile(r"[.!?\n;]+")
_COMP_MONEY = re.compile(
    r"(?:(?P<sym>[$£€])\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>k\b|thousand\b|m\b|million\b)?",
    re.I,
)
_CURRENCY_CODE = re.compile(r"\b(usd|gbp|eur|cad|aud|inr|sgd|chf|sek|nok)\b", re.I)
_SYMBOL_CURRENCY = {"$": "USD", "£": "GBP", "€": "EUR"}
# "up to 220k" states a ceiling and no floor; "at least 180k" the reverse.
_CEILING_ONLY = re.compile(
    r"\b(up to|as much as|max(?:imum)?(?: of)?|not exceeding|capped at)\b", re.I
)
_FLOOR_ONLY = re.compile(
    r"\b(at least|starting at|start(?:ing)? from|minimum(?: of)?|north of"
    r"|upwards of|or more|and up)\b",
    re.I,
)
# Annual pay outside this band is a typo, an hourly rate, or not pay at all.
_COMP_FLOOR_BOUND = 20_000.0
_COMP_CEILING_BOUND = 10_000_000.0


def parse_compensation(text: str) -> tuple[float | None, float | None, str]:
    """Stated annual pay from prose. Returns ``(minimum, maximum, currency)``.

    Only the first pay clause yielding a plausible figure is used. A single
    stated figure fills both ends, because "the salary is $200,000" states a
    ceiling as much as a floor; "up to" and "at least" fill one end only.

    Conservative by construction: ``jobmatch.preference`` vetoes a match on a
    stated ceiling, so a false positive here silently drops a real role.
    """
    for clause in _COMP_CLAUSE.split(text or ""):
        if not _COMP_CUE.search(clause) or _COMP_NOISE.search(clause):
            continue
        amounts, currency = _pay_amounts(clause)
        if not amounts:
            continue
        low, high = min(amounts), max(amounts)
        if len(amounts) == 1:
            if _CEILING_ONLY.search(clause):
                return None, high, currency
            if _FLOOR_ONLY.search(clause):
                return low, None, currency
        return low, high, currency
    return None, None, ""


def _pay_amounts(clause: str) -> tuple[list[float], str]:
    cleaned = _RETIREMENT_PLAN.sub(" ", clause)
    code = _CURRENCY_CODE.search(cleaned)
    currency = code.group(1).upper() if code else ""
    amounts: list[float] = []
    for match in _COMP_MONEY.finditer(cleaned):
        symbol = match.group("sym")
        if symbol and not currency:
            currency = _SYMBOL_CURRENCY.get(symbol, "")
        value = _annualise(match.group("num"), match.group("unit"))
        if value is not None:
            amounts.append(value)
    return amounts, currency


def _annualise(raw: str, unit: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    suffix = (unit or "").lower()
    if suffix.startswith(("k", "thousand")):
        value *= 1_000
    elif suffix.startswith(("m", "million")):
        value *= 1_000_000
    elif value < 1_000:
        # "paying 180" means 180k. Below 1000 with no unit is shorthand or noise.
        value *= 1_000
    return value if _COMP_FLOOR_BOUND <= value <= _COMP_CEILING_BOUND else None


# --------------------------------------------------------------------------
# heuristic parser
# --------------------------------------------------------------------------

# The alternation includes an en dash on purpose: recruiters paste year ranges
# out of job descriptions, where word processors have replaced the hyphen.
_YEARS_RANGE_RE = re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:yrs?|years?)")  # noqa: RUF001
_YEARS_MIN_RE = re.compile(
    r"(?:at least|min(?:imum)?(?: of)?|over|more than|\bgt\b)?\s*(\d{1,2})\s*\+?\s*(?:yrs?|years?)"
)
_YEARS_MAX_RE = re.compile(r"(?:under|less than|at most|max(?:imum)?(?: of)?)\s*(\d{1,2})\s*(?:yrs?|years?)")
_LOCATION_RE = re.compile(r"\b(?:in|near|around|based in|located in|from)\s+([a-z][a-z .'-]{2,40})")
_COMPANY_RE = re.compile(r"\b(?:ex-|from|worked at|currently at|at)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*)?)")
_NEGATION_RE = re.compile(r"\b(?:not|no|exclude|excluding|without)\s+([a-z][a-z0-9+#./ -]{2,30})")

_DEGREE_TOKENS = {
    "phd": "phd", "ph d": "phd", "doctorate": "phd",
    "ms": "ms", "msc": "ms", "masters": "ms", "master": "ms",
    "bs": "bs", "bsc": "bs", "bachelors": "bs", "bachelor": "bs", "ba": "bs",
    "mba": "mba",
}

_REMOTE_TOKENS = {"remote", "remotely", "distributed", "work from home", "wfh"}

# Words recruiters type in every query. They carry no matching signal, so they
# must never become scored keywords.
_RECRUITING_STOPWORDS = {
    "looking", "look", "find", "finding", "need", "needs", "want", "wants", "seeking",
    "seek", "hire", "hiring", "recruit", "recruiting", "someone", "somebody", "people",
    "person", "candidate", "candidates", "profile", "profiles", "background", "backgrounds",
    "experience", "experienced", "expertise", "skills", "skilled", "strong", "solid",
    "great", "good", "best", "top", "ideal", "ideally", "preferably", "must", "should",
    "years", "year", "yrs", "yoe", "plus", "role", "roles", "job", "jobs", "position",
    "team", "teams", "company", "companies", "startup", "startups", "work", "working",
    "worked", "who", "with", "and", "for", "the", "any", "all", "please", "give",
    "show", "list", "engineer", "engineers", "developer", "developers", "manager",
    "managers", "designer", "designers", "scientist", "scientists",
    "know", "knows", "knowing", "familiar", "using", "use", "used", "built",
    "build", "building", "shipped", "ship", "based", "currently",
}


def _find_titles(slug: str) -> list[str]:
    """Longest alias first so "machine learning engineer" beats "engineer"."""
    padded = f" {slug} "
    hits: list[str] = []
    aliases = sorted(tx.TITLE_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)
    consumed = padded
    for alias, canonical in aliases:
        # Recruiters type plurals ("machine learning engineers"); match both.
        for needle in (f" {alias} ", f" {alias}s "):
            if needle in consumed:
                if canonical not in hits:
                    hits.append(canonical)
                consumed = consumed.replace(needle, " ")
                break
    for family in tx.TITLE_FAMILIES:
        if family in hits:
            continue
        if f" {family} " in padded or f" {family}s " in padded:
            hits.append(family)
    return hits


def _find_seniority(slug: str) -> list[str]:
    padded = f" {slug} "
    found: list[str] = []
    for level in tx.SENIORITY_ORDER:
        if f" {level} " in padded and level not in found:
            found.append(level)
    for alias, canonical in tx.SENIORITY_ALIASES.items():
        if f" {alias} " in padded and canonical not in found:
            found.append(canonical)
    return found


_NEGATION_CUES = ("not", "no", "outside", "excluding", "exclude", "without", "except", "besides")


def _is_negated(slug: str, term: str) -> bool:
    """True when ``term`` is preceded by a negation cue within a few words.

    Catches "not in the US" / "outside London". Anything subtler is what the LLM
    parser is for — this only has to avoid the confidently-wrong reading.
    """
    position = slug.find(term)
    if position < 0:
        return False
    preceding = slug[max(0, position - 40) : position].split()
    return any(cue in preceding[-4:] for cue in _NEGATION_CUES)


def _find_locations(query: str, slug: str) -> list[str]:
    found: list[str] = []
    padded = f" {slug} "
    for name in list(tx.METRO_MEMBERS) + list(tx.LOCATION_ALIASES):
        if f" {name} " in padded and not _is_negated(slug, name):
            canonical = tx.normalize_location(name)
            if canonical not in found:
                found.append(canonical)
    for match in _LOCATION_RE.finditer(query.lower()):
        candidate = tx.normalize_location(match.group(1).strip(" .'-"))
        # Guard against "in python" style false positives.
        if (
            candidate
            and candidate not in found
            and tx.normalize_skill(candidate) not in tx.KNOWN_SKILLS
            and not _is_negated(slug, candidate)
            and (candidate in tx.METRO_MEMBERS or candidate in tx.LOCATION_ALIASES.values())
        ):
            found.append(candidate)
    return found


def _find_years(slug: str) -> tuple[float | None, float | None]:
    range_match = _YEARS_RANGE_RE.search(slug)
    if range_match:
        low, high = float(range_match.group(1)), float(range_match.group(2))
        return (min(low, high), max(low, high))
    max_match = _YEARS_MAX_RE.search(slug)
    max_years = float(max_match.group(1)) if max_match else None
    min_match = _YEARS_MIN_RE.search(slug)
    min_years = float(min_match.group(1)) if min_match else None
    if max_years is not None and min_years is not None and min_years == max_years:
        min_years = None
    return min_years, max_years


def heuristic_parse(query: str) -> ParsedQuery:
    """Deterministic fallback parser — no network, no model."""
    slug = tx.slugify_text(query)
    padded = f" {slug} "

    min_years, max_years = _find_years(slug)
    skills = tx.extract_skills_from_text(query)
    titles = _find_titles(slug)

    negatives: list[str] = []
    for match in _NEGATION_RE.finditer(slug):
        term = match.group(1).strip()
        if term:
            negatives.append(term.split()[0] if len(term.split()) > 3 else term)

    remote = True if any(f" {token} " in padded for token in _REMOTE_TOKENS) else None

    industries = [
        industry
        for industry in {tx.normalize_industry(i) for i in tx.INDUSTRY_ALIASES.values()}
        | set(tx.INDUSTRY_ALIASES)
        if f" {industry} " in padded
    ]
    industries = _dedupe(tx.normalize_industry(i) for i in industries)

    stages = [stage for stage in tx.COMPANY_STAGES if f" {stage} " in padded]
    sizes = [size for size in tx.COMPANY_SIZE_BUCKETS if size in slug]

    degrees = _dedupe(
        canonical for token, canonical in _DEGREE_TOKENS.items() if f" {token} " in padded
    )

    companies = _dedupe(
        tx.normalize_company(m.group(1))
        for m in _COMPANY_RE.finditer(query)
        if tx.normalize_company(m.group(1))
        and tx.normalize_company(m.group(1)) not in tx.KNOWN_SKILLS
    )
    # "at least 5 years" and friends must not become company names.
    companies = [c for c in companies if not c.split()[0].isdigit() and c not in {"least", "most"}]

    locations = _find_locations(query, slug)
    seniority = _find_seniority(slug)

    # Anything already captured as structure, plus recruiting boilerplate, must
    # not resurface as a keyword — otherwise "engineers in San Francisco" scores
    # people on whether the word "san" appears in their profile.
    structural: set[str] = set(STOPWORDS) | _RECRUITING_STOPWORDS | set(tx.SENIORITY_ORDER)
    structural.update(word for token in _REMOTE_TOKENS for word in token.split())
    structural.update(("onsite", "hybrid", "office", "relocate", "relocation"))
    for group in (titles, skills, industries, stages, degrees, locations, seniority, companies):
        for item in group:
            structural.update(item.split())
    for alias, canonical in tx.SENIORITY_ALIASES.items():
        structural.update(alias.split())
        structural.add(canonical)

    keywords = _dedupe(
        t
        for t in slug.split()
        if len(t) > 2 and t not in structural and not t.isdigit()
    )[:6]

    return ParsedQuery(
        job_titles=titles,
        must_have_skills=skills,
        seniority=seniority,
        min_years_experience=min_years,
        max_years_experience=max_years,
        locations=locations,
        remote_ok=remote,
        industries=industries,
        company_stages=stages,
        company_sizes=sizes,
        target_companies=companies,
        degrees=degrees,
        keywords=keywords,
        negative_keywords=_dedupe(negatives),
        search_text=slug,
    ).normalized()


# --------------------------------------------------------------------------
# llm parser
# --------------------------------------------------------------------------


def llm_parse(query: str) -> ParsedQuery:
    llm = get_llm()
    result = llm.json(
        system=QUERY_PARSER_SYSTEM,
        prompt=query_parser_prompt(query),
        schema=QUERY_PARSER_SCHEMA,
        effort="low",
        max_tokens=8000,
    )
    if not result.data:
        raise LLMUnavailable("parser returned no data")
    return ParsedQuery.from_dict(result.data).normalized()


def parse_query(
    query: str, *, overrides: dict[str, Any] | None = None, use_llm: bool | None = None
) -> tuple[ParsedQuery, str]:
    """Returns the parsed query and which parser produced it."""
    llm = get_llm()
    want_llm = llm.available if use_llm is None else use_llm

    parsed: ParsedQuery | None = None
    parser_name = "heuristic"
    if want_llm and llm.available:
        try:
            parsed = llm_parse(query)
            parser_name = "llm"
        except Exception as exc:  # noqa: BLE001 - degrade, never fail the search
            logger.warning("llm query parse failed, using heuristics: %s", exc)

    if parsed is None:
        parsed = heuristic_parse(query)

    if not parsed.search_text:
        parsed.search_text = tx.slugify_text(query)

    # A model that returns an empty structure is worse than the heuristics.
    if parser_name == "llm" and parsed.is_empty():
        fallback = heuristic_parse(query)
        if not fallback.is_empty():
            parsed, parser_name = fallback, "heuristic"

    return parsed.merge_overrides(overrides), parser_name

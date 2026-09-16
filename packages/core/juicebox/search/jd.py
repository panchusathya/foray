"""Job descriptions as search input.

A recruiter pastes the JD they were sent. That text is not a search sentence:
it carries an EEO clause, a benefits list, a company blurb, and a "nice to
have" section that names a dozen tools nobody is expected to know. Handing it
to ``search.parser.heuristic_parse`` turns all of that into requirements --
every taxonomy skill lands in ``must_have_skills``, the hard-miss penalty in
``scoring.assess`` floors every candidate to the same 0.45x, and the ranking
collapses into retrieval noise.

So the JD gets its own compiler, and it is deliberately conservative: a short
capped set of musts, everything else demoted to nice-to-have, and boilerplate
dropped rather than scored. ``jobmatch.spec`` compiles a stored ``JobPosting``
through this same code, so the posting path and the paste-a-JD path cannot
drift apart.
"""

from __future__ import annotations

import logging
import re

from ..enrich import taxonomy as tx
from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import JD_PARSER_SCHEMA, JD_PARSER_SYSTEM, jd_parser_prompt
from .parser import (
    ParsedQuery,
    _find_locations,
    _find_years,
    heuristic_parse,
    parse_compensation,
)

logger = logging.getLogger(__name__)

# A JD names tools it would like; it does not mean it needs all of them. The
# cap is what stops ``assess`` flooring the whole corpus.
MAX_MUST_HAVES = 5

# Enough text that it cannot be a search sentence. A recruiter's longest real
# query ("staff backend engineers at fintech companies who know Go and Kafka,
# ideally with payments experience, SF or remote") is ~140 characters.
_JD_MIN_CHARS = 320

# Headings a job description has and a search sentence never does.
_JD_MARKERS = re.compile(
    r"^\s*(?:what you(?:'| wi)ll do|what you will do|responsibilities|the role"
    r"|about (?:the role|us|the team|the company)|requirements|qualifications"
    r"|who you are|what we(?:'re| are) looking for|must have|nice to have"
    r"|preferred qualifications|benefits|perks|compensation|what we offer"
    r"|equal opportunity|how to apply|our stack|minimum qualifications)\b",
    re.I | re.M,
)
# Written as escapes, not literals: several of these render
# indistinguishably from "-" in a source file, and a JD pasted out of
# Word uses one of them.
_BULLET_GLYPHS = "-*\u2022\u2023\u25e6\u2043\u2219"
_BULLET = re.compile(rf"^\s*(?:[{re.escape(_BULLET_GLYPHS)}]|\d+[.)])\s+", re.M)

# Lines that are pure boilerplate. Dropped before the text becomes retrieval
# input, so BM25 stops matching people on "unlimited PTO".
_BOILERPLATE_LINE = re.compile(
    r"\b(equal opportunity|e\.?o\.?e\b|affirmative action|without regard to"
    r"|protected (?:veteran|class)|reasonable accommodation|background check"
    r"|401\s*\(?k\)?|unlimited pto|paid time off|parental leave|dental"
    r"|vision insurance|health insurance|wellness stipend|commuter"
    r"|no agencies|principals only|recruiters please|how to apply"
    r"|apply (?:now|today|via|through)|we are committed to|our mission is"
    r"|drug[- ]free workplace|at[- ]will employment|visa sponsorship"
    r"|we do not sponsor)\b",
    re.I,
)


def looks_like_job_description(text: str) -> bool:
    """True when this paste should be compiled as a JD, not read as a query.

    Length alone is not the test -- a recruiter pasting three sentences of
    context is still asking a question. A JD announces itself with section
    headings or a bulleted requirement list.
    """
    stripped = (text or "").strip()
    if len(stripped) < _JD_MIN_CHARS:
        return False
    if _JD_MARKERS.search(stripped):
        return True
    # No recognisable headings: fall back to shape. Several bullets across
    # several lines is a posting; a long run-on sentence is not.
    return len(_BULLET.findall(stripped)) >= 3 and stripped.count("\n") >= 4


def strip_boilerplate(text: str) -> str:
    """Drop the lines a JD contains for legal and HR reasons, not hiring ones."""
    kept = [line for line in (text or "").splitlines() if not _BOILERPLATE_LINE.search(line)]
    return "\n".join(kept)


def infer_title(text: str) -> str:
    """The advertised role, read off the top of the paste.

    A pasted JD has no ``JobPosting.title`` column to read, and the title is
    what seeds ``must_have_skills`` -- without it every skill in the document
    looks equally central.
    """
    for line in (text or "").splitlines():
        candidate = line.strip(" \t#*-•")
        if not candidate or len(candidate) > 120:
            continue
        # Skip a company banner or a location line masquerading as the title.
        if _BOILERPLATE_LINE.search(candidate):
            continue
        normalized = tx.normalize_title(candidate)
        if normalized and normalized != tx.normalize_title(""):
            return candidate
    return ""


# The header is the title, the employer, and the location, in some order.
# Wider than that and "our offices in London, NYC and Berlin" becomes three
# location requirements on a San Francisco role.
_HEADER_LINES = 4


def infer_location(text: str) -> str:
    """The work location, read off the header of the paste.

    A stored posting carries ``JobPosting.location``; a paste carries it in
    whatever line the employer put it on. Without this the location criterion
    never fires and a local role ranks the whole world equally.
    """
    header = "\n".join((text or "").splitlines()[:_HEADER_LINES])
    if not header.strip():
        return ""
    locations, _ = _find_locations(header, tx.slugify_text(header))
    return ", ".join(locations)


def title_adjacent_skills(title_normalized: str, jd_text: str) -> tuple[list[str], list[str]]:
    """Split the JD's skills into a capped must set and everything else.

    A skill earns "must" by sharing a token with the title, or by neighbouring
    one that does. Everything the document merely mentions is nice-to-have.
    """
    title_tokens = {tok for tok in tx.slugify_text(title_normalized).split() if tok}
    jd_skills = tx.extract_skills_from_text(f"{title_normalized} {jd_text}")
    must: list[str] = []
    for skill in jd_skills:
        skill_tokens = {tok for tok in tx.normalize_skill(skill).split() if tok}
        if skill_tokens & title_tokens:
            must.append(skill)
    seeded = list(must)
    for skill in seeded:
        for neighbour in tx.SKILL_NEIGHBOURS.get(tx.normalize_skill(skill), set()):
            if neighbour in jd_skills and neighbour not in must:
                must.append(neighbour)
    must = must[:MAX_MUST_HAVES]
    nice = [skill for skill in jd_skills if skill not in must]
    return must, nice


def cap_must_haves(parsed: ParsedQuery) -> ParsedQuery:
    """Demote musts past the cap rather than dropping them."""
    extra = parsed.must_have_skills[MAX_MUST_HAVES:]
    parsed.must_have_skills = parsed.must_have_skills[:MAX_MUST_HAVES]
    if extra:
        parsed.nice_to_have_skills = list(dict.fromkeys([*parsed.nice_to_have_skills, *extra]))
    return parsed


def apply_stated_comp(parsed: ParsedQuery, jd_text: str) -> None:
    """Fill the pay range from the JD when the caller has not already.

    Read off the raw JD: the structured path only ever sees title and
    location, so without this the field is never populated and nothing
    downstream has a figure to test.
    """
    if parsed.min_compensation is not None or parsed.max_compensation is not None:
        return
    low, high, currency = parse_compensation(jd_text or "")
    parsed.min_compensation = low
    parsed.max_compensation = high
    if currency:
        parsed.compensation_currency = currency


def heuristic_jd_parse(
    jd_text: str,
    *,
    title: str = "",
    location: str = "",
    seniority: str = "",
) -> ParsedQuery:
    """Offline JD compiler: title + location drive structure, the body only skills.

    The body is never fed to ``heuristic_parse``. That is the whole point --
    its negation scan reads "we do not sponsor visas" as a negative keyword
    (a hard filter), and its skill extractor treats the nice-to-have list as
    requirements.
    """
    title = title or infer_title(jd_text)
    location = location or infer_location(jd_text)
    body = strip_boilerplate(jd_text or "")

    parsed = heuristic_parse(f"{title} {location}".strip() or title)
    title_normalized = tx.normalize_title(title)
    must, nice = title_adjacent_skills(title_normalized, body)
    parsed.must_have_skills = must
    parsed.nice_to_have_skills = nice

    if seniority and seniority not in parsed.seniority:
        parsed.seniority = [seniority, *parsed.seniority]

    # Experience floors live in the requirements list, which the title-and-
    # location parse never sees. Without this a "5+ years" JD ranks graduates
    # alongside the people it asked for.
    if parsed.min_years_experience is None and parsed.max_years_experience is None:
        min_years, max_years = _find_years(tx.slugify_text(body))
        parsed.min_years_experience = min_years
        parsed.max_years_experience = max_years

    # Retrieval text is the de-boilerplated body, not the raw paste: BM25 and
    # the embedding should match on the role, not on the benefits package.
    parsed.search_text = tx.slugify_text(f"{title} {location} {body}")
    apply_stated_comp(parsed, jd_text)
    return parsed.normalized()


def llm_jd_parse(
    jd_text: str,
    *,
    title: str = "",
    location: str = "",
    company: str = "",
    seniority: str = "",
) -> ParsedQuery:
    llm = get_llm()
    result = llm.json(
        system=JD_PARSER_SYSTEM,
        prompt=jd_parser_prompt(
            title=title or infer_title(jd_text),
            location=location,
            company=company,
            seniority=seniority,
            jd_text=jd_text or "",
        ),
        schema=JD_PARSER_SCHEMA,
        effort="low",
        max_tokens=8000,
    )
    if not result.data:
        raise LLMUnavailable("JD parser returned no data")
    parsed = ParsedQuery.from_dict(result.data)
    # The model skips a stated range often enough that leaving it to the model
    # alone reproduces the bug this field exists to fix.
    apply_stated_comp(parsed, jd_text)
    return parsed.normalized()


def parse_job_description(
    jd_text: str,
    *,
    title: str = "",
    location: str = "",
    company: str = "",
    seniority: str = "",
    use_llm: bool | None = None,
) -> tuple[ParsedQuery, str]:
    """Compile a job description into a query. Returns the parser that won."""
    llm = get_llm()
    want_llm = llm.available if use_llm is None else use_llm
    if want_llm and llm.available:
        try:
            parsed = llm_jd_parse(
                jd_text,
                title=title,
                location=location or infer_location(jd_text),
                company=company,
                seniority=seniority,
            )
            if not parsed.is_empty():
                return cap_must_haves(parsed).normalized(), "llm_jd"
        except Exception as exc:  # noqa: BLE001 - degrade, never fail a search
            logger.warning("llm JD parse failed, using heuristics: %s", exc)
    return (
        heuristic_jd_parse(jd_text, title=title, location=location, seniority=seniority),
        "heuristic_jd",
    )

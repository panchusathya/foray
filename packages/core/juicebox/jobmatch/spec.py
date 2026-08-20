"""Compile a JobPosting into a ParsedQuery.

The LLM path is conservative: invented constraints are the expensive failure.
The offline path never feeds a full JD to ``heuristic_parse`` — that would dump
every taxonomy skill into must_have_skills and floor scores via the hard-miss
penalty in ``assess``.
"""

from __future__ import annotations

import logging

from ..enrich import taxonomy as tx
from ..llm.client import LLMUnavailable, get_llm
from ..llm.prompts import JD_PARSER_SCHEMA, JD_PARSER_SYSTEM, jd_parser_prompt
from ..models import JobPosting
from ..search.parser import ParsedQuery, heuristic_parse

logger = logging.getLogger(__name__)

MAX_MUST_HAVES = 5


def job_spec(posting: JobPosting) -> tuple[ParsedQuery, str]:
    """Return a normalised ParsedQuery and the parser that produced it."""
    llm = get_llm()
    if llm.available:
        try:
            parsed = _llm_jd_parse(posting)
            if not parsed.is_empty():
                return _cap_must_haves(parsed).normalized(), "llm"
        except Exception as exc:  # noqa: BLE001 - degrade, never fail a parse
            logger.warning("llm JD parse failed, using heuristics: %s", exc)
    return heuristic_job_spec(posting), "heuristic"


def heuristic_job_spec(posting: JobPosting) -> ParsedQuery:
    """Title + location + seniority only; JD skills are nice-to-have / capped musts."""
    title = posting.title or ""
    location = posting.location or ""
    parsed = heuristic_parse(f"{title} {location}".strip() or title)
    title_normalized = posting.title_normalized or tx.normalize_title(title)
    must, nice = _title_adjacent_skills(title_normalized, posting.jd_text or "")
    parsed.must_have_skills = must
    parsed.nice_to_have_skills = nice
    if posting.seniority and posting.seniority not in parsed.seniority:
        parsed.seniority = [posting.seniority, *parsed.seniority]
    remainder = tx.slugify_text(f"{title} {location} {posting.jd_text or ''}")
    parsed.search_text = remainder
    return parsed.normalized()


def _title_adjacent_skills(title_normalized: str, jd_text: str) -> tuple[list[str], list[str]]:
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


def _cap_must_haves(parsed: ParsedQuery) -> ParsedQuery:
    extra = parsed.must_have_skills[MAX_MUST_HAVES:]
    parsed.must_have_skills = parsed.must_have_skills[:MAX_MUST_HAVES]
    if extra:
        parsed.nice_to_have_skills = list(
            dict.fromkeys([*parsed.nice_to_have_skills, *extra])
        )
    return parsed


def _llm_jd_parse(posting: JobPosting) -> ParsedQuery:
    llm = get_llm()
    result = llm.json(
        system=JD_PARSER_SYSTEM,
        prompt=jd_parser_prompt(
            title=posting.title or "",
            location=posting.location or "",
            company=posting.company or "",
            seniority=posting.seniority or "",
            jd_text=posting.jd_text or "",
        ),
        schema=JD_PARSER_SCHEMA,
        effort="low",
        max_tokens=8000,
    )
    if not result.data:
        raise LLMUnavailable("JD parser returned no data")
    return ParsedQuery.from_dict(result.data).normalized()

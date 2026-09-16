"""Compile a JobPosting into a ParsedQuery.

The compiler itself lives in ``search.jd``, because a pasted job description
and a scraped posting are the same problem: a document full of boilerplate
that has to become a small, honest set of requirements. This module is the
JobPosting-shaped door into it, so the two callers cannot drift apart.
"""

from __future__ import annotations

from ..enrich import taxonomy as tx
from ..models import JobPosting
from ..search.jd import (
    MAX_MUST_HAVES,
    apply_stated_comp,
    cap_must_haves,
    heuristic_jd_parse,
    llm_jd_parse,
    parse_job_description,
)
from ..search.parser import ParsedQuery

__all__ = [
    "MAX_MUST_HAVES",
    "heuristic_job_spec",
    "job_spec",
]


def job_spec(posting: JobPosting) -> tuple[ParsedQuery, str]:
    """Return a normalised ParsedQuery and the parser that produced it."""
    parsed, parser = parse_job_description(
        posting.jd_text or "",
        title=posting.title or "",
        location=posting.location or "",
        company=posting.company or "",
        seniority=posting.seniority or "",
    )
    # The posting path has always reported "llm"/"heuristic", and both
    # spec_version_for and the stored parser column read those values.
    return parsed, "llm" if parser == "llm_jd" else "heuristic"


def heuristic_job_spec(posting: JobPosting) -> ParsedQuery:
    """Title + location + seniority only; JD skills are nice-to-have / capped musts."""
    return heuristic_jd_parse(
        posting.jd_text or "",
        title=posting.title or tx.normalize_title(posting.title_normalized or ""),
        location=posting.location or "",
        seniority=posting.seniority or "",
    )


def _llm_jd_parse(posting: JobPosting) -> ParsedQuery:
    """Retained for callers and tests that reach for the model path directly."""
    return llm_jd_parse(
        posting.jd_text or "",
        title=posting.title or "",
        location=posting.location or "",
        company=posting.company or "",
        seniority=posting.seniority or "",
    )


def _cap_must_haves(parsed: ParsedQuery) -> ParsedQuery:
    return cap_must_haves(parsed)


def _apply_stated_comp(parsed: ParsedQuery, posting: JobPosting) -> None:
    apply_stated_comp(parsed, posting.jd_text or "")

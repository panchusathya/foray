"""Hard filters — the small set of constraints that remove people outright.

Everything else is scored rather than filtered. Recruiters lose more from a
silently-dropped good profile than from a slightly-off one ranked 12th, so the
list here is deliberately short and each rule carries tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..enrich import taxonomy as tx
from .index import CandidateDoc
from .parser import ParsedQuery

# How far below a stated minimum we still allow through (people round down their
# own experience, and our own year computation is approximate).
YEARS_MIN_TOLERANCE = 0.75
YEARS_MAX_TOLERANCE = 1.0


@dataclass
class FilterOutcome:
    mask: np.ndarray
    excluded: dict[str, int] = field(default_factory=dict)

    @property
    def eligible_count(self) -> int:
        return int(self.mask.sum())


def _matches_company(doc: CandidateDoc, wanted: set[str]) -> bool:
    if not wanted:
        return False
    names = {doc.current_company_normalized} | {
        entry.get("company_normalized", "") for entry in doc.companies
    }
    names.discard("")
    return bool(names & wanted)


def _matches_title_exclusion(doc: CandidateDoc, excluded: list[str]) -> bool:
    current = doc.current_title_normalized
    for title in excluded:
        if not title:
            continue
        if current and tx.title_similarity(current, title) >= 0.95:
            return True
    return False


# States that count as "on the market" when the caller asks to see only those.
# "passive" is in: a stale yes is still a better bet than silence, and the
# caller who ticked this box wants a shortlist, not a certainty.
AVAILABLE_STATES = {"active", "open", "passive"}


def apply_filters(
    docs: list[CandidateDoc], parsed: ParsedQuery, *, only_available: bool = False
) -> FilterOutcome:
    mask = np.ones(len(docs), dtype=bool)
    excluded: dict[str, int] = {}

    excluded_companies = {tx.normalize_company(c) for c in parsed.excluded_companies if c}
    excluded_companies.discard("")
    negative_keywords = [k for k in parsed.negative_keywords if k]
    min_years = parsed.min_years_experience
    max_years = parsed.max_years_experience

    def reject(index: int, reason: str) -> None:
        mask[index] = False
        excluded[reason] = excluded.get(reason, 0) + 1

    for index, doc in enumerate(docs):
        # Off by default, and deliberately so: most people in a database have
        # never been asked, and "unknown" is not "no".
        if only_available and doc.availability.state not in AVAILABLE_STATES:
            reject(index, "not_on_market")
            continue
        if excluded_companies and _matches_company(doc, excluded_companies):
            reject(index, "excluded_company")
            continue
        if parsed.title_exclusions and _matches_title_exclusion(doc, parsed.title_exclusions):
            reject(index, "excluded_title")
            continue
        if negative_keywords:
            haystack = f" {doc.haystack} "
            if any(f" {keyword} " in haystack for keyword in negative_keywords):
                reject(index, "negative_keyword")
                continue
        if min_years is not None and doc.years_experience < min_years * YEARS_MIN_TOLERANCE:
            reject(index, "below_min_years")
            continue
        if max_years is not None and doc.years_experience > max_years + YEARS_MAX_TOLERANCE:
            reject(index, "above_max_years")
            continue

    return FilterOutcome(mask=mask, excluded=excluded)

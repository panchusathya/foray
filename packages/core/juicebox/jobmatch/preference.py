"""Compare a candidate preference spec to a posting spec.

Three states, not two:

- ``None`` — no preference, so ``score_pair`` keeps today's two-term blend
- scored 0-1 plus criteria — titles, seniority, location/remote, stage/size/
  industry, comp band
- veto at ``0.0`` with a veto criterion — ``excluded_companies``, or a
  stated posting *ceiling* below the candidate's floor

``None`` on a hard miss would silently keep the old score. ``0.0`` lets
``_upsert_match`` drop ``new`` rows below the floor while leaving
shortlisted and dismissed rows in place.

Most postings do not state compensation. Vetoing on absence would empty
the board, so a missing figure is not a veto.

The comparison is against ``ParsedQuery.max_compensation`` — the top of the
stated band — because that is the number a candidate's floor has to clear. A
posting that states only a floor ("from 180k") could pay more, so it has no
ceiling to fail and is never vetoed. Mismatched currencies are not compared at
all: converting them would need a rate, and guessing one turns a bad rate into
a dropped role.
"""

from __future__ import annotations

from typing import Any

from ..enrich import taxonomy as tx
from ..models import CandidatePreference, JobPosting
from ..search.parser import ParsedQuery

# Bumped whenever the term changes shape, so every posting rescores rather
# than keeping a score the current code would not produce. v2: the comp
# comparison reads ParsedQuery.max_compensation, which the JD parser now
# actually fills -- under v1 it read keys ParsedQuery cannot emit, so the
# comp veto never fired.
JOB_MATCH_ENGINE_VERSION = "pref-v2"


def preference_term(
    pref: CandidatePreference | None,
    spec: ParsedQuery,
    *,
    posting: JobPosting | None = None,
) -> tuple[float | None, list[dict]]:
    if pref is None or not _has_signal(pref):
        return None, []

    wanted = ParsedQuery.from_dict(pref.spec or {}).normalized()
    have = spec.normalized()
    criteria: list[dict] = []

    excluded = _exclusion_veto(wanted, posting)
    if excluded is not None:
        return 0.0, [excluded]

    floor = pref.target_total or pref.target_base
    ceiling = _posting_ceiling(pref, have)
    if floor and ceiling is not None and ceiling < float(floor):
        return 0.0, [
            _criterion(
                "comp_floor",
                "Compensation floor",
                0.0,
                f"Prefers {floor:.0f}; posting tops out at {ceiling:.0f}",
                veto=True,
            )
        ]

    parts: list[float] = []
    title = _title_fit(wanted.job_titles, have.job_titles or _posting_titles(posting))
    if title is not None:
        parts.append(title)
        criteria.append(_criterion("pref_title", "Preferred titles", title, ""))
    seniority = _overlap(wanted.seniority, have.seniority or _as_list(getattr(posting, "seniority", "")))
    if seniority is not None:
        parts.append(seniority)
        criteria.append(_criterion("pref_seniority", "Preferred seniority", seniority, ""))
    location = _location_fit(wanted, have, posting)
    if location is not None:
        parts.append(location)
        criteria.append(_criterion("pref_location", "Preferred location", location, ""))
    remote = _remote_fit(wanted, have, posting)
    if remote is not None:
        parts.append(remote)
        criteria.append(_criterion("pref_remote", "Remote preference", remote, ""))
    for name, label, left, right in (
        ("pref_industry", "Preferred industry", wanted.industries, have.industries),
        ("pref_stage", "Preferred stage", wanted.company_stages, have.company_stages),
        ("pref_size", "Preferred size", wanted.company_sizes, have.company_sizes),
        ("pref_skills", "Must-have skills", wanted.must_have_skills, have.must_have_skills),
    ):
        score = _overlap(left, right)
        if score is not None:
            parts.append(score)
            criteria.append(_criterion(name, label, score, ""))
    if floor and ceiling is not None and ceiling >= float(floor):
        parts.append(1.0)
        criteria.append(
            _criterion("pref_comp", "Compensation", 1.0, f"Posting tops out at {ceiling:.0f}")
        )

    if not parts:
        return None, []
    return max(0.0, min(1.0, sum(parts) / len(parts))), criteria


def _has_signal(pref: CandidatePreference) -> bool:
    spec = pref.spec or {}
    if any(spec.get(key) not in (None, "", [], {}) for key in (
        "job_titles",
        "seniority",
        "locations",
        "must_have_skills",
        "industries",
        "company_stages",
        "company_sizes",
        "excluded_companies",
        "target_companies",
    )):
        return True
    if spec.get("remote_ok") is not None:
        return True
    return bool(pref.target_base or pref.target_total)


def _exclusion_veto(wanted: ParsedQuery, posting: JobPosting | None) -> dict | None:
    blocked = {tx.normalize_company(name) for name in wanted.excluded_companies if name}
    blocked.discard("")
    if not blocked or posting is None:
        return None
    company = tx.normalize_company(posting.company_normalized or posting.company or "")
    if company and company in blocked:
        return _criterion(
            "excluded_company",
            "Excluded company",
            0.0,
            f"Prefers not to work at {posting.company or company}",
            veto=True,
        )
    return None


def _posting_ceiling(pref: CandidatePreference, have: ParsedQuery) -> float | None:
    """Top of the posting's stated band, or ``None`` when it cannot be compared.

    ``None`` for three distinct reasons, all of which must not veto: the posting
    states no pay, it states only a floor, or its currency differs from the
    candidate's.
    """
    if not _currency_comparable(pref.comp_currency, have.compensation_currency):
        return None
    return _as_number(have.max_compensation)


def _currency_comparable(left: str, right: str) -> bool:
    """Blank on either side means unknown, which we treat as comparable.

    Most records say nothing, and refusing to compare those would make the
    ceiling test dead again. Two *different* stated currencies is the case worth
    refusing.
    """
    a = (left or "").strip().upper()
    b = (right or "").strip().upper()
    if not a or not b:
        return True
    return a == b


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _title_fit(wanted: list[str], have: list[str]) -> float | None:
    if not wanted:
        return None
    if not have:
        return 0.0
    best = 0.0
    for target in wanted:
        norm_target = tx.normalize_title(target)
        for actual in have:
            best = max(best, tx.title_similarity(tx.normalize_title(actual), norm_target))
    return best


def _overlap(wanted: list[str], have: list[str], *, normalize=tx.slugify_text) -> float | None:
    left = {normalize(item) for item in wanted if item}
    right = {normalize(item) for item in have if item}
    left.discard("")
    right.discard("")
    if not left:
        return None
    if not right:
        return 0.0
    return len(left & right) / len(left)


def _location_fit(wanted: ParsedQuery, have: ParsedQuery, posting: JobPosting | None) -> float | None:
    have_locs = list(have.locations)
    if posting is not None and posting.location:
        have_locs.append(posting.location)
    return _overlap(wanted.locations, have_locs, normalize=tx.normalize_location)


def _remote_fit(wanted: ParsedQuery, have: ParsedQuery, posting: JobPosting | None) -> float | None:
    if wanted.remote_ok is None:
        return None
    posting_remote = bool(have.remote_ok) or bool(getattr(posting, "open_to_remote", False))
    if wanted.remote_ok:
        return 1.0 if posting_remote else 0.0
    return 1.0 if not posting_remote else 0.4


def _posting_titles(posting: JobPosting | None) -> list[str]:
    if posting is None:
        return []
    return [value for value in (posting.title_normalized, posting.title) if value]


def _as_list(value: str) -> list[str]:
    return [value] if value else []


def _criterion(name: str, label: str, score: float, evidence: str, *, veto: bool = False) -> dict:
    status = "miss" if score < 0.4 else ("partial" if score < 0.8 else "strong")
    if veto:
        status = "miss"
    return {
        "name": name,
        "label": label,
        "kind": "preference",
        "weight": 1.0,
        "status": status,
        "score": round(score, 4),
        "evidence": evidence,
        "sources": ["intake"],
        "veto": veto,
    }

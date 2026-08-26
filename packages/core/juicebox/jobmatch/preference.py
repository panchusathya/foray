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
a dropped role — but "UNKNOWN" is the sentinel for a figure given with no
currency, not a currency, and reading it as one made the veto dead for every
heuristic-path extraction.
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
# comp veto never fired. v4: the veto reads the "UNKNOWN" currency sentinel as
# unstated rather than as a second stated currency, and tests the posting's
# band against the candidate's *base* target -- so both the rows it wrongly
# kept and the rows it wrongly dropped rescore.
JOB_MATCH_ENGINE_VERSION = "pref-v4"

# What each dimension is worth. A flat mean was the bug: a bullseye on title
# and a total title miss both came out 0.500 for the same candidate, because
# seniority and location could pay back everything title lost. Title is the
# thing the candidate actually asked for, so it outweighs the rest combined;
# the soft demographics of a company are tiebreakers, not gates.
_WEIGHTS = {
    "pref_title": 3.0,
    "pref_location": 1.5,
    "pref_seniority": 1.0,
    "pref_remote": 1.0,
    "pref_comp": 1.0,
    "pref_skills": 1.0,
    "pref_industry": 0.5,
    "pref_stage": 0.5,
    "pref_size": 0.5,
}

# Adjacent rungs are worth showing; two apart is a stretch; beyond that it is
# a different job.
_SENIORITY_DECAY = {0: 1.0, 1: 0.6, 2: 0.25}


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

    floor = _comp_floor(pref)
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

    parts: list[tuple[float, float]] = []

    def add(name: str, label: str, score: float | None, evidence: str = "") -> None:
        if score is None:
            return
        weight = _WEIGHTS[name]
        parts.append((weight, score))
        criteria.append(_criterion(name, label, score, evidence, weight=weight))

    add(
        "pref_title",
        "Preferred titles",
        _title_fit(wanted.job_titles, have.job_titles or _posting_titles(posting)),
    )
    add(
        "pref_seniority",
        "Preferred seniority",
        _seniority_fit(
            wanted.seniority, have.seniority or _as_list(getattr(posting, "seniority", ""))
        ),
    )
    add("pref_location", "Preferred location", _location_fit(wanted, have, posting))
    add("pref_remote", "Remote preference", _remote_fit(wanted, have, posting))
    add("pref_industry", "Preferred industry", _overlap(_real_industries(wanted), have.industries))
    add("pref_stage", "Preferred stage", _overlap(wanted.company_stages, have.company_stages))
    add("pref_size", "Preferred size", _overlap(wanted.company_sizes, have.company_sizes))
    add(
        "pref_skills",
        "Must-have skills",
        _overlap(wanted.must_have_skills, have.must_have_skills),
    )
    if floor and ceiling is not None and ceiling >= float(floor):
        add("pref_comp", "Compensation", 1.0, f"Posting tops out at {ceiling:.0f}")

    if not parts:
        return None, []
    total = sum(weight for weight, _ in parts)
    return max(0.0, min(1.0, sum(w * s for w, s in parts) / total)), criteria


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


def _comp_floor(pref: CandidatePreference) -> float | None:
    """The candidate's base target, falling back to total.

    Base first because the number it is tested against is read off phrases like
    "base salary range" -- a base band. Preferring the larger of the two figures
    vetoed a 160-190k base band for somebody whose base target was 150k and
    whose *total* target was 200k, and ``score_pair`` drops a vetoed row: a role
    paying above their own base target disappeared silently. Where only a total
    is on file it is the one figure we have.
    """
    return pref.target_base or pref.target_total


def _posting_ceiling(pref: CandidatePreference, have: ParsedQuery) -> float | None:
    """Top of the posting's stated band, or ``None`` when it cannot be compared.

    ``None`` for three distinct reasons, all of which must not veto: the posting
    states no pay, it states only a floor, or its currency differs from the
    candidate's.
    """
    if not _currency_comparable(pref.comp_currency, have.compensation_currency):
        return None
    return _as_number(have.max_compensation)


#: ``intake.extract`` writes this rather than guessing USD when a figure
#: arrives with no stated currency (see ``CandidatePreference.comp_currency``).
#: Reading it as a stated currency is how every heuristic-path extraction
#: compared "UNKNOWN" against "USD", found two different currencies, and
#: skipped the comp veto entirely.
_UNSTATED_CURRENCIES = {"", "UNKNOWN"}


def _currency_comparable(left: str, right: str) -> bool:
    """An unstated currency on either side is comparable.

    Most records say nothing, and refusing to compare those would make the
    ceiling test dead again. Two *different* stated currencies is the case worth
    refusing.
    """
    a = (left or "").strip().upper()
    b = (right or "").strip().upper()
    if a in _UNSTATED_CURRENCIES or b in _UNSTATED_CURRENCIES:
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


def _seniority_fit(wanted: list[str], have: list[str]) -> float | None:
    """Distance on the ladder, not set membership.

    ``_overlap`` scored a stated "junior" against an Associate role tagged
    ``mid`` as a hard zero -- which is how a correctly-levelled role came out
    a miss and dragged a perfect title match below the floor with it.
    """
    if not wanted:
        return None
    if not have:
        return 0.0
    best = 0.0
    for target in wanted:
        norm_target = tx.normalize_seniority(target)
        for actual in have:
            distance = tx.seniority_distance(tx.normalize_seniority(actual), norm_target)
            if distance is None:
                continue
            best = max(best, _SENIORITY_DECAY.get(abs(distance), 0.0))
    return best


def _real_industries(wanted: ParsedQuery) -> list[str]:
    """Industries, minus anything that is the job function restated.

    Intake extraction routinely copies the target function into ``industries``
    -- "strategic finance" arrives as both a job title and an industry. No
    posting lists a function as its industry, so the duplicate is a guaranteed
    miss on every role in the board. Counting it once, as a title, is the
    honest reading of what the candidate said.
    """
    titles = {tx.slugify_text(item) for item in wanted.job_titles if item}
    titles.discard("")
    return [item for item in wanted.industries if tx.slugify_text(item) not in titles]


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
    """Best fit between where they want to work and where the role sits.

    ``tx.location_match``, not set equality. Places are written down half a
    dozen ways and equality scored all but one of them zero: a candidate who
    said "San Francisco Bay Area" against a posting in "San Francisco, CA"
    came out a hard miss, while ``search.scoring`` -- reading the same two
    strings -- called it 0.65. Two comparators disagreeing about the same
    pair is the bug; the metro-aware one is the right side of the
    disagreement.
    """
    have_locs = list(have.locations)
    if posting is not None and posting.location:
        have_locs.append(posting.location)
    wanted_locs = [loc for loc in wanted.locations if loc]
    if not wanted_locs:
        return None
    have_locs = [loc for loc in have_locs if loc]
    if not have_locs:
        return 0.0
    # Both orientations. ``location_match`` looks the *target* up in the metro
    # table and nothing else, so "Menlo Park" scores zero against a stated
    # "San Francisco" while "San Francisco" scores 0.75 against a stated
    # "Menlo Park" -- the same two places, and which one wins depends on which
    # argument we happened to pass first.
    return max(
        max(tx.location_match(have_locs, loc) for loc in wanted_locs),
        max(tx.location_match(wanted_locs, loc) for loc in have_locs),
    )


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


def _criterion(
    name: str,
    label: str,
    score: float,
    evidence: str,
    *,
    veto: bool = False,
    weight: float = 1.0,
) -> dict:
    status = "miss" if score < 0.4 else ("partial" if score < 0.8 else "strong")
    if veto:
        status = "miss"
    return {
        "name": name,
        "label": label,
        "kind": "preference",
        "weight": weight,
        "status": status,
        "score": round(score, 4),
        "evidence": evidence,
        "sources": ["intake"],
        "veto": veto,
    }
